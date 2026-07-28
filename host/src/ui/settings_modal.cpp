// Chudware Settings modal (Appearance / General / AI / Providers / Benchmarks /
// Prompts / Agent / Experimental tabs) and the About modal.
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "settings_modal.h"
#include "tool_panel_tables.h"  // generated offline fallback tool catalog (tools/gen_tool_panel_tables.py)
#include "config_panel.h"
#include "layout.h"  // drawConfigurationsBody fallback

#include "../core/app_state.h"
#include "file_dialog.h"
#include "../core/fs_backend.h"
#include "../core/cpp_core.h"
#include "../renderer.h"
#include "copilot/copilot.h"  // ui::drawAiSettingsSection (glass-grouped AI tab sections)
#include "ai/ai_prefs.h"  // ui::aiUiVerbosity/aiFollowActiveSession/aiActiveWorkPreviews
#include "model_registry.h"  // ui::localPresets (experimental vision-review model presets)
#include "leaderboard_ui.h"  // aicad.leaderboard/1 compact table (Settings > Benchmarks)
#include "oauth_capture.h"  // in-app xAI multi-account CEF OAuth (Settings → Accounts)
#include "web_capabilities.h"  // ui::webGapReason (T-RF12: sign-in is desktop-only on web)
#include "../core/async_rpc.h"  // core::RpcResult for skills/triggers RPCs
#include "widgets/interwebz.h"  // Skills/Context two-column editors
#include "ui/provider_logos.h"  // ui::drawProviderLogo/providerBrandColor (Providers list badges)
#include "provider_usage.h"  // ui::UsageAccount/UsageWindow + THE subscription_* poller
#include "metrics.h"  // ui::metrics::px/stroke — DPI-scaled Settings gutter + usage rings
#include "tesseract.h"  // ui::drawTesseract + TesseractAnim — Appearance > Brand mark picker
#include "../ui_theme.h"  // ui::busyLabel — the one inline "working" indicator


// ---- C22b prompt editor state ----
// Per-role editor buffer and metadata, lazily loaded from get_prompts_v2.
namespace {
struct PromptRecord {
    std::string role;
    std::string builtinPath;     // abs path of the built-in .md
    std::string overridePath;    // abs path where we write the override
    bool overridden = false;
    // 16 KB buffer (matches the card spec)
    std::vector<char> buf{std::vector<char>(16 * 1024, 0)};
    int builtinLen = 0;  // length of the built-in text (for diff indicator)
};
static std::vector<PromptRecord> s_promptRecs;
static bool s_promptsLoaded = false;
static int  s_selectedRole  = 0;

static std::string s_promptsError;
static void loadPrompts(AppState& st) {
    // Prefer list_editable_prompts (YAML mission roles + core .md, with overrides).
    // Fall back to get_prompts_v2 (C++ core kRoles catalog) when the worker is old.
    nlohmann::json result;
    std::string err;
    s_promptsError.clear();
    bool ok = st.worker.call("list_editable_prompts", nlohmann::json::object(), result, err, 20000);
    if (!ok) {
        err.clear();
        ok = st.worker.call("get_prompts_v2", nlohmann::json::object(), result, err, 20000);
        if (!ok) {
            s_promptsError = err.empty() ? "prompt catalog unavailable" : err;
            st.addLog("[err] prompts catalog: " + s_promptsError);
            s_promptsLoaded = false;
            return;
        }
    }
    // Bare array or {"prompts":[...]} or error object
    auto str = [](const nlohmann::json& j, const char* k) -> std::string {
        auto it = j.find(k);
        return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
    };
    if (result.is_object() && result.value("ok", true) == false) {
        s_promptsError = str(result, "error");
        if (s_promptsError.empty()) s_promptsError = "prompt catalog error";
        s_promptsLoaded = false;
        return;
    }
    const nlohmann::json* recs = &result;
    if (result.is_object() && result.contains("prompts") && result["prompts"].is_array())
        recs = &result["prompts"];
    s_promptRecs.clear();
    if (!recs->is_array()) { s_promptsLoaded = true; return; }
    for (auto& r : *recs) {
        if (!r.is_object()) continue;
        PromptRecord pr;
        pr.role         = str(r, "role");
        if (pr.role.empty()) continue;
        pr.builtinPath  = str(r, "path");
        pr.overridePath = str(r, "override_path");
        pr.overridden   = r.value("overridden", false);
        std::string text = str(r, "text");
        pr.builtinLen = (int)text.size();
        std::fill(pr.buf.begin(), pr.buf.end(), 0);
        std::strncpy(pr.buf.data(), text.c_str(), pr.buf.size() - 1);
        s_promptRecs.push_back(std::move(pr));
    }
    // Stable alpha order for power-user scanning
    std::sort(s_promptRecs.begin(), s_promptRecs.end(),
              [](const PromptRecord& a, const PromptRecord& b) { return a.role < b.role; });
    s_promptsLoaded = true;
}


static void savePromptOverride(PromptRecord& pr) {
    // Write the buffer contents to the override file
    std::ofstream f(pr.overridePath, std::ios::out | std::ios::trunc);
    if (f.is_open()) {
        f << pr.buf.data();
        pr.overridden = true;
    }
}

static void resetPromptOverride(PromptRecord& pr, AppState& st) {
    // Delete the override file
    std::remove(pr.overridePath.c_str());
    pr.overridden = false;
    // Reload built-in text from the worker
    s_promptsLoaded = false;
    loadPrompts(st);
}

// A dimmed "(?)" marker that reveals a one-line explanation on hover — for discoverability of the
// less-obvious settings (vision / plan mode / max iters / units). Drawn inline (SameLine first).
static void helpMarker(const char* desc) {
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                 ui::palette.textDim[2], 1));
    ImGui::TextUnformatted("(?)");
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 28.0f);
        ImGui::TextUnformatted(desc);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

// Shared Settings section chrome: accent title + even vertical rhythm across every tab.
// Prefer this over raw ImGui::SeparatorText so Appearance/General/AI/… share one look.
static void settingsSectionHeader(const char* title, bool first = false) {
    const float sc = ui::uiScale;
    ImGui::Dummy(ImVec2(0, first ? 0.0f : 6.0f * sc));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
    ImGui::TextUnformatted(title);
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 1.0f * sc));
}

// Frosted glass card around a group of related fields. Auto-height; id must be unique per tab.
// Keeps label+widget pairs inside one child so scrolling cannot detach a caption from its field.
static void settingsGlassSectionBegin(const char* id, const char* title) {
    const float sc = ui::uiScale;
    settingsSectionHeader(title);
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10.0f * sc, 7.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(8.0f * sc, 5.0f * sc));
    ImGui::BeginChild(id, ImVec2(0, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
}

static void settingsGlassSectionEnd() {
    ImGui::EndChild();
    ImGui::PopStyleVar(2);  // ItemSpacing, WindowPadding
    ui::popGlassChildStyle();
}

// Window-local accent pill scrollbar for a scrolling child inside the Settings modal. Call
// immediately before EndChild(); it is a no-op when the content fits. One adaptivePanel id for
// the whole modal — adaptivePanel is idempotent per frame, so repeated fetches are free.
static void settingsPillScrollbar() {
    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
    ui::drawWindowPillScrollbarV(ui::adaptivePanel(ui::panelId("settings_modal_glass"),
                                                  ImRect(mn, mx), ui::GlassGroup::Global));
}

// Sticky Settings tabs: tab bar stays put; only the body child scrolls.
// ImGuiChildFlags_AlwaysUseWindowPadding is REQUIRED: a child created with _None ignores
// WindowPadding entirely, so the padding pushed here used to be a no-op and every tab drew
// flush against the modal edge — which is exactly why each section grew its own ad-hoc indent.
// One gutter lives here now. Every body carries ImGuiWindowFlags_NoScrollbar and paints the
// app-wide accent pill in settingsTabBodyEnd instead of ImGui's grey bar (a default bar
// anywhere is a bug), so there is no reserved right-hand gutter to pin a ScrollbarSize to.
// NoScrollWithMouse stays reserved for scroll=false bodies — adding it to a scrolling body
// would silently eat the wheel.
static void settingsTabBodyBegin(const char* id, bool scroll = true) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
    ImGuiWindowFlags flags = ImGuiWindowFlags_NoScrollbar;
    if (!scroll) flags |= ImGuiWindowFlags_NoScrollWithMouse;
    ImGui::BeginChild(id, ImVec2(0, 0), ImGuiChildFlags_AlwaysUseWindowPadding, flags);
}
static void settingsTabBodyEnd() {
    settingsPillScrollbar();
    ImGui::EndChild();
    ImGui::PopStyleVar();  // WindowPadding
}

// AI sub-tabs whose bodies own their own scrolling (Skills / Context) must zero the PARENT
// tab-body scroll exactly once — on the frame the sub-tab becomes active. Doing it every frame
// (the old behavior) hard-pins the parent, so the wheel over those tabs was silently eaten.
// Only the SELECTED sub-tab's body runs, so "previously-active index" plus a frame-gap test
// covers both switching between two reporting sub-tabs and coming back from one that doesn't
// report (Harness/Roles/…) or from a whole different Settings tab.
static int s_aiSubTabPrev  = -1;     // sub-tab index whose body last drew
static int s_aiSubTabFrame = -100;   // frame it drew on
static bool aiSubTabJustEntered(int idx) {
    const int frame = ImGui::GetFrameCount();
    const bool entered = (s_aiSubTabPrev != idx) || (frame - s_aiSubTabFrame) > 1;
    s_aiSubTabPrev  = idx;
    s_aiSubTabFrame = frame;
    return entered;
}


// Apply a dark/light theme immediately (settings.theme persists on Save) and sync the viewport
// background. Shared by the General-tab theme control. MUST pass st.scale: applyTheme re-seeds all
// style-vars to base and only multiplies them by the scale argument (ScaleAllSizes) — passing 1.0f
// on a HiDPI display collapses every padding/rounding to 100% while the pre-scaled fonts stay large,
// and clobbers ui::uiScale, desyncing every custom-drawn widget. (Was hardcoded 1.0f — bug.)
static void applyThemeNow(AppState& st, bool light) {
    ui::Theme next = light ? ui::Theme::Light : ui::Theme::Dark;
    ui::applyTheme(next, st.scale);
    st.settings.theme = light ? "light" : "dark";
    // Persist immediately: the checkbox is labeled "saved immediately" and the View-menu copy
    // already saves on toggle — without this, toggle-then-Cancel applied the theme in memory
    // but lost it on restart (audit P1).
    saveSettings(st.settings);
    if (st.renderer)
        st.renderer->setBackground(
            ui::palette.viewportTop[0], ui::palette.viewportTop[1], ui::palette.viewportTop[2],
            ui::palette.viewportBottom[0], ui::palette.viewportBottom[1],
            ui::palette.viewportBottom[2]);
}
// ---- Subscription accounts (Settings → Providers → Accounts) ----------------
// Drives subscription_switch / subscription_login / subscription_logout. The model, the
// parsing and the subscription_accounts/subscription_status/subscription_usage polls live in
// ui/provider_usage.h: the Copilot composer draws the SAME windows as rings, and a second
// poller here would double the RPC traffic and drift out of sync. Only presentation is local.
// Usage stays optional per contracts/providers.md §4.5 — absent fields degrade to a dim
// "usage unavailable" row.

// Which frame this section last drew — a pure UI concern (it edge-detects "section became
// visible" to force one usage poll), so it stays here rather than in the shared poll state.
static int s_usageLastDrawnFrame = -100;

// Optimistic overlays. providerUsageAccounts() is shared read-only state, so a switch or a
// sign-out can no longer be applied to a row in place. These hold the user's click until the
// forced refetch lands — without them the radio snaps back / the signed-out row reappears for
// the length of one RPC round trip, which reads as "the click did nothing".
static std::string s_pendingActiveProv;   // provider whose selection is mid-switch
static std::string s_pendingActiveId;     // account_id the user picked
static std::string s_pendingSignOutKey;   // "<provider>\n<account_id>" being removed

// CEF add-account disabled — device/browser Python flow (xai_sub_provider)
// handles multi-account storage+rotation. Leave oauth_capture compiled but dormant.
static constexpr bool kXaiUseCef = false;

static const char* kSubProviders[] = {"xai", "claude", "codex", "cursor", "kimi"};
static const char* kSubProviderLabels[] = {
    "xAI Grok", "Claude", "ChatGPT / Codex", "Cursor", "Kimi Code"};
// Brisk accounts refresh while this (visible) management section owns the poll.
static constexpr double kAccountsPollIntervalSec = 1.5;

static std::string optionalJsonString(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
}
// Present-but-null / wrong-type fields must not throw under WASM (exception catching
// disabled → page abort). Default when key missing, null, or not the requested type.
static bool optionalJsonBool(const nlohmann::json& obj, const char* key, bool dflt = false) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    if (it->is_boolean()) return it->get<bool>();
    return dflt;
}
static int optionalJsonInt(const nlohmann::json& obj, const char* key, int dflt = 0) {
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    if (it->is_number_integer()) return it->get<int>();
    if (it->is_number()) return (int)it->get<double>();
    return dflt;
}
static nlohmann::json optionalJsonObj(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_object()) return nlohmann::json::object();
    return *it;
}
static const nlohmann::json* optionalJsonArr(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_array()) return nullptr;
    return &(*it);
}
// A rate-limit reset stamp: ISO-8601 from the Python worker, but the omp bridge sends it as a
// NUMBER of milliseconds since the epoch. optionalJsonString() saw the number, returned empty,
// and the countdown never rendered. ui::usageResetCountdown() reads both forms, so keep the ms
// value verbatim as its decimal digits.
static std::string optionalJsonStamp(const nlohmann::json& obj, const char* key) {
    auto it = obj.find(key);
    if (it == obj.end()) return {};
    if (it->is_string()) return it->get<std::string>();
    if (it->is_number()) return std::to_string((long long)it->get<double>());
    return {};
}

// Shorten a long account id / UUID for the primary label (never a full 36-char UUID).
static std::string shortAccountId(const std::string& id) {
    if (id.empty()) return "(account)";
    if (id.size() <= 10) return id;
    return id.substr(0, 8) + "\xe2\x80\xa6";
}

// Primary row label: display_name → email/label → shortened account id.
static std::string accountPrimaryLabel(const ui::UsageAccount& row) {
    if (!row.displayName.empty()) return row.displayName;
    if (!row.label.empty()) return row.label;
    return shortAccountId(row.accountId);
}

// Small plan pill (chipBg fill + accent border/text). No-op when plan empty.
static void drawPlanChip(const std::string& plan, float sc) {
    if (plan.empty()) return;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 ts = ImGui::CalcTextSize(plan.c_str());
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  h  = ts.y + 4.0f * sc;
    const float  wd = ts.x + 12.0f * sc;
    const auto& pal = ui::palette;
    dl->AddRectFilled(p, ImVec2(p.x + wd, p.y + h),
                      ui::Palette::toU32(pal.chipBg), h * 0.5f);
    dl->AddRect(p, ImVec2(p.x + wd, p.y + h),
                ImGui::ColorConvertFloat4ToU32(
                    ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.55f)),
                h * 0.5f, 0, 1.0f * sc);
    dl->AddText(ImVec2(p.x + 6.0f * sc, p.y + 2.0f * sc),
                ui::Palette::toU32(pal.accent), plan.c_str());
    ImGui::Dummy(ImVec2(wd, h));
}

// One usage window: circular gauge + label over "NN%  resets in …". The ring is the shared
// ui::drawFractionRing primitive, so this readout and the composer's usage indicators are
// literally the same shape — the old flat rectangular bar read as an unrelated widget.
static void drawUsageWindow(const ui::UsageWindow& w, float sc) {
    if (!w.hasFraction) {
        const char* detail = "usage unavailable";
        if (w.status == "needs_login") detail = "sign in required";
        else if (w.status == "blocked") detail = "browser verification required";
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::Text("%s \xe2\x80\x94 %s", w.label.c_str(), detail);
        ImGui::PopStyleColor();
        return;
    }
    const float* role = ui::usageStatusColor(w.status, w.usedFraction);
    const ImU32 roleU = ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 1.0f));

    const std::string countdown = ui::usageResetCountdown(w.resetsAt);
    char sub[96];
    if (!countdown.empty())
        std::snprintf(sub, sizeof(sub), "%.0f%%  %s", w.usedFraction * 100.0f, countdown.c_str());
    else
        std::snprintf(sub, sizeof(sub), "%.0f%%", w.usedFraction * 100.0f);

    // Ring left, two stacked lines right, both centred against the ring. Sizing the row from
    // the taller of the two keeps consecutive windows on an even rhythm whether or not a
    // countdown is present.
    const float thick   = ui::metrics::stroke(3.0f);
    const float radius  = ImGui::GetFrameHeight() * 0.42f;
    const float ringBox = radius * 2.0f + thick;
    const float lineH   = ImGui::GetTextLineHeight();
    const float lineGap = 2.0f * sc;
    const float textH   = lineH * 2.0f + lineGap;
    const float rowH    = ImMax(ringBox, textH);

    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::drawFractionRing(dl, ImVec2(p.x + ringBox * 0.5f, p.y + rowH * 0.5f),
                         radius, thick, w.usedFraction,
                         ui::Palette::toU32(ui::palette.accentSoft), roleU);

    const float textX = p.x + ringBox + 8.0f * sc;
    const float textY = p.y + (rowH - textH) * 0.5f;
    dl->AddText(ImVec2(textX, textY), roleU, w.label.c_str());
    dl->AddText(ImVec2(textX, textY + lineH + lineGap),
                ui::Palette::toU32(ui::palette.textDim), sub);
    ImGui::Dummy(ImVec2(ImGui::GetContentRegionAvail().x, rowH));
}

static void drawSubscriptionAccounts(AppState& st, float sc) {
    dimText("Named subscription accounts per provider. Sign in adds another account; "
            "the radio selects which credential the harness uses (seamless switch).");

    // Edge-detect section open (frame gap) → force a usage poll; while visible,
    // refresh every 5 min per contracts/providers.md §4.5.
    const int frame = ImGui::GetFrameCount();
    const bool justOpened = (frame - s_usageLastDrawnFrame) > 1;
    s_usageLastDrawnFrame = frame;
    ui::providerUsagePollAccounts(st, kAccountsPollIntervalSec, false);
    ui::providerUsagePollUsage(st, justOpened);

    // Retire the optimistic overlays as soon as the refetched rows agree with them (or the
    // row vanished entirely — a rejected switch against a revoked account).
    if (!s_pendingActiveProv.empty() || !s_pendingSignOutKey.empty()) {
        bool activeSeen = false, signOutSeen = false;
        for (const auto& r : ui::providerUsageAccounts()) {
            if (r.provider == s_pendingActiveProv && r.accountId == s_pendingActiveId) {
                activeSeen = true;
                if (r.active) { s_pendingActiveProv.clear(); s_pendingActiveId.clear(); }
            }
            if (r.provider + "\n" + r.accountId == s_pendingSignOutKey) signOutSeen = true;
        }
        if (!activeSeen) { s_pendingActiveProv.clear(); s_pendingActiveId.clear(); }
        if (!signOutSeen) s_pendingSignOutKey.clear();
    }

    // In-app xAI OAuth modal (CEF). Draw every frame so the approve page keeps
    // receiving frames/input even if Accounts scrolls out of view.
    ui::oauthCapture().draw(st, sc);

    if (!ui::providerUsageError().empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", ui::providerUsageError().c_str());
        ImGui::PopStyleColor();
    }

    for (int pi = 0; pi < 5; ++pi) {
        const char* prov = kSubProviders[pi];
        const char* plabel = kSubProviderLabels[pi];
        ImGui::PushID(prov);
        ImGui::Dummy(ImVec2(0, 6.0f * sc));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1.0f));
        ImGui::TextUnformatted(plabel);
        ImGui::PopStyleColor();

        // Rows for this provider. The list is shared read-only state owned by
        // provider_usage.h, so nothing below mutates a row: clicks go out as RPCs and land on
        // the forced refetch, with the pending overlays covering the round trip.
        bool any = false;
        const std::vector<ui::UsageAccount>& accts = ui::providerUsageAccounts();
        for (size_t i = 0; i < accts.size(); ++i) {
            const ui::UsageAccount& row = accts[i];
            if (row.provider != prov) continue;
            const std::string rowKey = row.provider + "\n" + row.accountId;
            if (rowKey == s_pendingSignOutKey) continue;  // logout in flight — already gone
            any = true;
            ImGui::PushID((int)i);

            // ---- identity row: radio | display_name/email | plan chip | Sign out ----
            // A pending switch on this provider owns the dot until the refetch confirms it.
            const bool pendingHere = (s_pendingActiveProv == row.provider);
            const bool active = pendingHere ? (row.accountId == s_pendingActiveId) : row.active;
            // Radio alone (empty label) — text drawn beside so we can chip + sign-out.
            if (ImGui::RadioButton("##active", active)) {
                if (!active) {
                    nlohmann::json p;
                    p["provider"] = prov;
                    p["account_id"] = row.accountId;
                    st.rpc.call("subscription_switch", p, [](const core::RpcResult& r) {
                        const bool rejected = r.result.is_object() &&
                                              r.result.value("ok", true) == false;
                        if (!r.ok || rejected) {
                            auto reason = rejected
                                ? optionalJsonString(r.result, "reason") : r.err;
                            ui::providerUsageSetError(
                                reason.empty() ? "account switch failed" : reason);
                        }
                        ui::providerUsageInvalidate();  // force refresh/rollback
                    });
                    // Seamless — no restart. Overlay holds the dot until the refetch lands.
                    s_pendingActiveProv = row.provider;
                    s_pendingActiveId   = row.accountId;
                }
            }
            ImGui::SameLine(0, 8.0f * sc);

            // Primary: display_name → email/label → shortened account id (never full UUID).
            // Reserve room for plan chip + Sign out so long labels never clip the actions.
            const char* soLabel = "Sign out";
            const float soW = ImGui::CalcTextSize(soLabel).x
                + ImGui::GetStyle().FramePadding.x * 2.0f;
            const float planChipW = row.plan.empty()
                ? 0.0f
                : (ImGui::CalcTextSize(row.plan.c_str()).x + 12.0f * sc);
            const float gap = 8.0f * sc;
            const float rightBlockW = soW
                + (planChipW > 0.0f ? planChipW + gap : 0.0f)
                + gap;
            const float labelMaxW = std::max(64.0f * sc,
                ImGui::GetContentRegionAvail().x - rightBlockW);

            std::string primary = accountPrimaryLabel(row);
            std::string secondary;
            if (!row.displayName.empty() && !row.label.empty()
                && row.label != row.displayName)
                secondary = row.label;
            if (!row.connected) {
                if (!secondary.empty()) secondary += "  (disconnected)";
                else primary += " (disconnected)";
            }

            // Primary label (ellipsized) + plan chip + Sign out on one baseline.
            ImGui::AlignTextToFramePadding();
            {
                const ImVec2 ts = ImGui::CalcTextSize(primary.c_str());
                if (ts.x > labelMaxW) {
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    const ImVec2 p0 = ImGui::GetCursorScreenPos();
                    const char* ell = "\xe2\x80\xa6";
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    const float clipW = std::max(ellW + 4.0f * sc, labelMaxW);
                    dl->PushClipRect(p0,
                        ImVec2(p0.x + clipW - ellW, p0.y + ts.y + 2.0f * sc), true);
                    dl->AddText(p0, ImGui::GetColorU32(ImGuiCol_Text), primary.c_str());
                    dl->PopClipRect();
                    dl->AddText(ImVec2(p0.x + clipW - ellW, p0.y),
                                ImGui::GetColorU32(ImGuiCol_Text), ell);
                    ImGui::Dummy(ImVec2(labelMaxW,
                                        std::max(ts.y, ImGui::GetTextLineHeight())));
                } else {
                    ImGui::TextUnformatted(primary.c_str());
                }
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                    if (!secondary.empty() && !row.accountId.empty())
                        ImGui::SetTooltip("%s\n%s\n%s", primary.c_str(), secondary.c_str(),
                                          row.accountId.c_str());
                    else if (!row.accountId.empty() && row.accountId != primary)
                        ImGui::SetTooltip("%s\n%s", primary.c_str(), row.accountId.c_str());
                    else if (!secondary.empty())
                        ImGui::SetTooltip("%s\n%s", primary.c_str(), secondary.c_str());
                }
            }

            if (!row.plan.empty()) {
                ImGui::SameLine(0, gap);
                ImGui::AlignTextToFramePadding();
                drawPlanChip(row.plan, sc);
            }

            ImGui::SameLine(0, 0.0f);
            {
                const float avail = ImGui::GetContentRegionAvail().x;
                if (avail > soW + 4.0f * sc)
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
                        auto reason = rejected
                            ? optionalJsonString(r.result, "reason") : r.err;
                        ui::providerUsageSetError(
                            reason.empty() ? "sign out failed" : reason);
                    }
                    ui::providerUsageInvalidate();
                });
                // The row cannot be erased (shared, read-only) — hide it until the forced
                // refetch drops it for real, which also retires this overlay.
                s_pendingSignOutKey = rowKey;
                ImGui::PopID();
                break;
            }

            // Optional email secondary line under the primary identity.
            if (!secondary.empty()) {
                ImGui::Indent(ImGui::GetFrameHeight() + gap);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                const ImVec2 sts = ImGui::CalcTextSize(secondary.c_str());
                const float secMax = std::max(48.0f * sc, ImGui::GetContentRegionAvail().x);
                if (sts.x > secMax) {
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    const ImVec2 sp = ImGui::GetCursorScreenPos();
                    const char* ell = "\xe2\x80\xa6";
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    dl->PushClipRect(sp,
                        ImVec2(sp.x + secMax - ellW, sp.y + sts.y + 2.0f * sc), true);
                    dl->AddText(sp, ui::Palette::toU32(ui::palette.textDim), secondary.c_str());
                    dl->PopClipRect();
                    dl->AddText(ImVec2(sp.x + secMax - ellW, sp.y),
                                ui::Palette::toU32(ui::palette.textDim), ell);
                    ImGui::Dummy(ImVec2(secMax, sts.y));
                } else {
                    ImGui::TextUnformatted(secondary.c_str());
                }
                ImGui::PopStyleColor();
                ImGui::Unindent(ImGui::GetFrameHeight() + gap);
            }

            // ---- usage bars (indented under the identity row) ----
            ImGui::Indent(ImGui::GetFrameHeight() + gap);
            ImGui::Dummy(ImVec2(0, 2.0f * sc));
            if (row.hasUsage && !row.usage.empty()) {
                for (size_t wi = 0; wi < row.usage.size(); ++wi) {
                    ImGui::PushID((int)wi);
                    drawUsageWindow(row.usage[wi], sc);
                    ImGui::PopID();
                }
            } else {
                // Degrade: fields absent, or the worker latched subscription_usage as unknown
                // (provider_usage.h then stops polling) — say which, don't fake a 0% ring.
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                ImGui::TextUnformatted(ui::providerUsageRpcMissing()
                                           ? "usage not reported by this runtime"
                                           : "usage unavailable");
                ImGui::PopStyleColor();
            }
            ImGui::Unindent(ImGui::GetFrameHeight() + gap);

            ImGui::PopID();
        }
        if (!any) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No accounts signed in");
            ImGui::PopStyleColor();
        }

        // Live login progress for this provider (device-code UX).
        // When the in-app CEF modal owns the xAI flow, suppress the inline
        // external-browser progress (modal shows code + page instead).
        ui::UsageLoginProgress& prog = ui::providerUsageLogin()[prov];
        const bool cefOwnsXai = (std::string(prov) == "xai") && kXaiUseCef && ui::oauthCapture().active();
        if (cefOwnsXai) {
            // Modal owns the flow; Python status still drives prog via the normal
            // poll above. Do not force-poll every frame (TTL + in-flight guard
            // would still thrash accounts RPCs). Closing mid-flow leaves the
            // Python device-code poll running — same as the external-browser path.
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Sign-in open in embedded browser\xe2\x80\xa6");
            ImGui::PopStyleColor();
        } else if (prog.inProgress) {
            if (prog.userCode.empty() && prog.verifyUrl.empty()) {
                ImGui::TextUnformatted("Opening browser\xe2\x80\xa6");
            } else {
                if (prog.browserOpened)
                    ImGui::TextUnformatted("Browser opened \xe2\x80\x94 approve access, then wait\xe2\x80\xa6");
                else
                    ImGui::TextUnformatted("Could not auto-open browser \xe2\x80\x94 open the link below:");
                if (!prog.userCode.empty()) {
                    ImGui::Text("Code: %s", prog.userCode.c_str());
                    ImGui::SameLine();
                    char copyId[64];
                    std::snprintf(copyId, sizeof(copyId), "Copy code##%s", prov);
                    if (ImGui::SmallButton(copyId))
                        ImGui::SetClipboardText(prog.userCode.c_str());
                }
                // Always surface a code-bearing URL for xAI (see provider_usage.cpp).
                std::string fallbackUrl = prog.verifyUrl;
                if (fallbackUrl.empty() && std::string(prov) == "xai") {
                    fallbackUrl = "https://accounts.x.ai/oauth2/device";
                    if (!prog.userCode.empty())
                        fallbackUrl += "?user_code=" + prog.userCode;
                }
                if (!fallbackUrl.empty()) {
                    ImGui::TextWrapped("%s", fallbackUrl.c_str());
                    char openId[64];
                    std::snprintf(openId, sizeof(openId), "Open in browser##%s", prov);
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
            // Allow abandoning a stuck device-code wait without restarting the app.
            // Python may keep polling until TTL; GUI just stops showing the busy state.
            char cancelId[64];
            std::snprintf(cancelId, sizeof(cancelId), "Cancel##login_%s", prov);
            if (ImGui::SmallButton(cancelId)) {
                prog.inProgress = false;
                prog.userCode.clear();
                prog.verifyUrl.clear();
                prog.browserOpened = false;
                prog.stage.clear();
                prog.lastError.clear();
                ui::providerUsageInvalidate();
            }
            ui::providerUsagePollAccounts(st, kAccountsPollIntervalSec, true);
        } else {
            // First account vs multi-account: label makes "already signed in" obvious.
            // T-RF12: on the web (WASM) build the token store + OAuth flows live in the
            // desktop engine worker, so the button renders disabled with the gap reason
            // as its tooltip instead of firing an RPC that can only fail.
            const char* webWhy = ui::webGapReason("account_signin");
            char addId[64];
            std::snprintf(addId, sizeof(addId),
                          any ? "Add another account##%s" : "Sign in##%s", prov);
            if (webWhy) ImGui::BeginDisabled();
            if (ImGui::SmallButton(addId)) {
                const bool isXai = std::string(prov) == "xai";
                // xAI: CEF path is gated by kXaiUseCef (currently off — Python
                // device/browser token store+rotation handles multi-account).
                // When enabled, prefer in-app CEF; fallback is external browser.
                if (isXai && kXaiUseCef && ui::OauthCapture::cefAvailable()) {
                    prog.inProgress = true;
                    prog.lastError.clear();
                    prog.userCode.clear();
                    prog.verifyUrl.clear();
                    prog.browserOpened = false;
                    prog.stage = "start";
                    ui::oauthCapture().beginXai(st);
                } else {
                    prog.inProgress = true;
                    prog.lastError.clear();
                    prog.userCode.clear();
                    prog.verifyUrl.clear();
                    prog.browserOpened = false;
                    prog.stage = "start";
                    nlohmann::json p;
                    p["provider"] = prov;
                    // xAI is device-code only. Codex uses its browser callback flow here;
                    // its headless device flow cannot surface a code through this UI.
                    if (isXai)
                        p["device"] = true;
                    st.rpc.call("subscription_login", p, [prov](const core::RpcResult& r) {
                        ui::UsageLoginProgress& pr = ui::providerUsageLogin()[prov];
                        if (!r.ok) {
                            pr.inProgress = false;
                            pr.lastError = r.err.empty() ? "login failed" : r.err;
                        } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                            pr.inProgress = false;
                            auto reason = optionalJsonString(r.result, "reason");
                            auto hint = optionalJsonString(r.result, "hint");
                            pr.lastError = reason.empty() ? "login failed" : reason;
                            if (!hint.empty()) pr.lastError += " \xe2\x80\x94 " + hint;
                        }
                        ui::providerUsageInvalidate();
                    });
                }
            }
            if (webWhy) ImGui::EndDisabled();
            // Disabled items need AllowWhenDisabled to still show the explaining tooltip.
            if (webWhy
                    ? ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)
                    : ImGui::IsItemHovered()) {
                if (webWhy)
                    ImGui::SetTooltip("%s", webWhy);
                else if (std::string(prov) == "xai" && kXaiUseCef && ui::OauthCapture::cefAvailable())
                    ImGui::SetTooltip(
                        any
                            ? "Add another %s account in the embedded browser "
                              "(session isolated for multi-account)."
                            : "Sign in to %s in the embedded browser.",
                        plabel);
                else
                    ImGui::SetTooltip(
                        any
                            ? "Add another %s subscription account (browser OAuth)."
                            : "Sign in to %s (browser OAuth). Existing accounts stay listed above.",
                        plabel);
            }
        }
        if (!prog.lastError.empty() && !prog.inProgress) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", prog.lastError.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PopID();
    }
}

// ---- OMP provider manager (Settings → Providers) — S-UI-PROV ----------------------
// Two-pane provider sign-in/manager per docs/omp-integration/research/design-provider-signin-ui.md
// §3.1, driven by the frozen libchudware C-ABI methods (OMP_INTEGRATION_PLAN.md RPC CONTRACT),
// which proxy the bun bridge auth_*/models_list methods. The UI holds no auth truth — it
// renders the last agent_provider_list/agent_provider_state response and re-polls on open +
// short TTL (mirrors drawSubscriptionAccounts above). Degrade: unknown_method (sibling slice
// mid-flight / older core) flips s_omp.available off permanently and the tab falls back to
// the legacy Provider & Model / Moonshot / Accounts sections.
struct OmpProviderRow {
    std::string id;            // OMP registry id ("anthropic", "kimi-code", "moonshot", ...)
    std::string label;
    std::string state;         // signed_out|key_set|oauth_connected|error|disabled
    std::string origin;        // runtime|config|oauth|api_key|env|fallback (empty = none)
    std::string envVar;        // when origin == "env"
    std::string error;         // last validate/login error text
    std::string consoleUrl;    // vendor key console deep link
    bool keyless = false;
    bool hasApiKey = false;    // authKinds contains "api_key"
    bool hasOAuth = false;     // authKinds contains "oauth"
    int modelsAvailable = 0;
};
struct OmpAccountRow {
    int credentialId = 0;
    std::string accountId;
    std::string email;
    std::string orgName;
};
struct OmpProvDetail {
    bool loaded = false;
    std::string state;
    std::string origin;
    std::string error;
    std::vector<OmpAccountRow> accounts;
    std::vector<ui::UsageWindow> usage;
    std::vector<std::string> usageNotes;
};
struct OmpOAuthFlow {
    bool active = false;
    bool pollInFlight = false;
    std::string handle;
    std::string provider;
    std::string status;        // starting|awaiting_auth|connected|error|cancelled
    std::string url;
    std::string launchUrl;
    std::string instructions;
    std::string progress;
    std::string error;
    double lastPoll = -1e9;
    char codeBuf[512] = "";
};
struct OmpProvUi {
    bool available = true;     // false after unknown_method → legacy fallback
    bool listInFlight = false;
    double lastListPoll = -1e9;
    std::vector<OmpProviderRow> rows;
    std::string selectedId;
    std::string lastError;
    OmpProvDetail detail;
    std::string detailFor;
    bool detailInFlight = false;
    double lastDetailPoll = -1e9;
    // API-key flow (per selected provider)
    char keyBuf[256] = "";
    std::string keyFor;
    bool keyBusy = false;
    std::string keyStatus;
    bool keyStatusErr = false;
    OmpOAuthFlow oauth;
    int lastDrawnFrame = -100;
};
static OmpProvUi s_omp;

static bool ompRpcAbsent(const core::RpcResult& r) {
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

// Claude stack + Kimi first (design brief), then signed-in providers, then the rest alpha.
static int ompProviderRank(const OmpProviderRow& r) {
    if (r.id == "anthropic") return 0;
    if (r.id == "kimi-code") return 1;
    if (r.id == "moonshot") return 2;
    if (r.state != "signed_out" && !r.state.empty()) return 3;
    return 4;
}

static void pollOmpProviderList(AppState& st, bool force) {
    if (!s_omp.available || s_omp.listInFlight) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_omp.lastListPoll) < 3.0) return;
    s_omp.lastListPoll = now;
    s_omp.listInFlight = true;
    st.rpc.call("agent_provider_list", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_omp.listInFlight = false;
        if (ompRpcAbsent(r)) {
            s_omp.available = false;   // permanent degrade → legacy sections
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            if (s_omp.lastError.empty())
                s_omp.lastError = r.err.empty() ? "provider list unavailable" : r.err;
            return;
        }
        if (!optionalJsonBool(r.result, "ok", true)) {
            s_omp.lastError = optionalJsonString(r.result, "error");
            if (s_omp.lastError.empty()) s_omp.lastError = "provider list failed";
            return;
        }
        s_omp.lastError.clear();
        const auto* arr = optionalJsonArr(r.result, "providers");
        if (!arr) return;
        s_omp.rows.clear();
        for (const auto& p : *arr) {
            if (!p.is_object()) continue;
            OmpProviderRow row;
            row.id = optionalJsonString(p, "id");
            if (row.id.empty()) continue;
            row.label = optionalJsonString(p, "label");
            if (row.label.empty()) row.label = row.id;
            row.state = optionalJsonString(p, "state");
            if (row.state.empty()) row.state = "signed_out";
            row.origin = optionalJsonString(p, "origin");
            row.envVar = optionalJsonString(p, "envVar");
            row.error = optionalJsonString(p, "error");
            row.consoleUrl = optionalJsonString(p, "consoleUrl");
            row.keyless = optionalJsonBool(p, "keyless", false);
            row.modelsAvailable = optionalJsonInt(p, "modelsAvailable", 0);
            if (const auto* kinds = optionalJsonArr(p, "authKinds")) {
                for (const auto& k : *kinds) {
                    if (!k.is_string()) continue;
                    const std::string ks = k.get<std::string>();
                    if (ks == "api_key") row.hasApiKey = true;
                    if (ks == "oauth") row.hasOAuth = true;
                }
            }
            s_omp.rows.push_back(std::move(row));
        }
        std::stable_sort(s_omp.rows.begin(), s_omp.rows.end(),
                         [](const OmpProviderRow& a, const OmpProviderRow& b) {
            const int ra = ompProviderRank(a), rb = ompProviderRank(b);
            if (ra != rb) return ra < rb;
            return a.label < b.label;
        });
        // Sole writer of the signed-in bridge set: anything with credentials
        // (key_set / oauth_connected / connected / error-with-creds) counts;
        // only a plain signed_out state excludes a provider.
        st.signedInBridgeProviders.clear();
        for (const auto& row : s_omp.rows)
            if (!row.state.empty() && row.state != "signed_out")
                st.signedInBridgeProviders.insert(row.id);
        if (s_omp.selectedId.empty() && !s_omp.rows.empty())
            s_omp.selectedId = s_omp.rows.front().id;
    });
}

static void fetchOmpProviderDetail(AppState& st, const std::string& id, bool force) {
    if (!s_omp.available || id.empty() || s_omp.detailInFlight) return;
    const double now = ImGui::GetTime();
    if (!force && s_omp.detailFor == id && (now - s_omp.lastDetailPoll) < 60.0) return;
    s_omp.lastDetailPoll = now;
    s_omp.detailInFlight = true;
    s_omp.detailFor = id;
    st.rpc.call("agent_provider_state", nlohmann::json{{"provider", id}},
                [id](const core::RpcResult& r) {
        s_omp.detailInFlight = false;
        if (ompRpcAbsent(r)) { s_omp.available = false; return; }
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true))
            return;   // soft fail — keep last detail
        if (s_omp.detailFor != id) return;   // selection moved on
        OmpProvDetail d;
        d.loaded = true;
        d.state = optionalJsonString(r.result, "state");
        d.origin = optionalJsonString(r.result, "origin");
        d.error = optionalJsonString(r.result, "error");
        if (const auto* accts = optionalJsonArr(r.result, "accounts")) {
            for (const auto& a : *accts) {
                if (!a.is_object()) continue;
                OmpAccountRow ac;
                ac.credentialId = optionalJsonInt(a, "credentialId", 0);
                ac.accountId = optionalJsonString(a, "accountId");
                ac.email = optionalJsonString(a, "email");
                ac.orgName = optionalJsonString(a, "orgName");
                d.accounts.push_back(std::move(ac));
            }
        }
        const nlohmann::json usage = optionalJsonObj(r.result, "usage");
        if (usage.is_object() && !usage.empty()) {
            if (const auto* notes = optionalJsonArr(usage, "notes"))
                for (const auto& n : *notes)
                    if (n.is_string()) d.usageNotes.push_back(n.get<std::string>());
            if (const auto* wins = optionalJsonArr(usage, "windows")) {
                for (const auto& w : *wins) {
                    if (!w.is_object()) continue;
                    ui::UsageWindow sw;
                    sw.window = optionalJsonString(w, "id");
                    sw.label = optionalJsonString(w, "label");
                    if (sw.label.empty()) sw.label = optionalJsonString(w, "windowLabel");
                    if (sw.label.empty()) sw.label = sw.window;
                    auto it = w.find("usedFraction");
                    if (it != w.end() && it->is_number()) {
                        sw.usedFraction = (float)it->get<double>();
                        sw.hasFraction = true;
                    }
                    sw.resetsAt = optionalJsonStamp(w, "resetsAt");
                    sw.status = optionalJsonString(w, "status");
                    if (sw.status.empty()) sw.status = "unknown";
                    d.usage.push_back(std::move(sw));
                }
            }
        }
        s_omp.detail = std::move(d);
    }, 15000);   // auth_state fetches usage reports upstream (8s bridge-side cap)
}

static void refreshOmpProvider(AppState& st) {
    s_omp.lastListPoll = -1e9;
    s_omp.lastDetailPoll = -1e9;
    pollOmpProviderList(st, true);
    fetchOmpProviderDetail(st, s_omp.selectedId, true);
}

// Parse one OAuth flow snapshot (shared by begin_oauth and oauth_poll results).
static void ompOAuthApplySnapshot(const nlohmann::json& r) {
    OmpOAuthFlow& f = s_omp.oauth;
    const std::string st_ = optionalJsonString(r, "status");
    if (!st_.empty()) f.status = st_;
    const std::string url = optionalJsonString(r, "url");
    if (!url.empty()) f.url = url;
    const std::string launch = optionalJsonString(r, "launchUrl");
    if (!launch.empty()) f.launchUrl = launch;
    const std::string instr = optionalJsonString(r, "instructions");
    if (!instr.empty()) f.instructions = instr;
    const std::string prog = optionalJsonString(r, "progress");
    if (!prog.empty()) f.progress = prog;
    const std::string err = optionalJsonString(r, "error");
    if (!err.empty()) f.error = err;
}

static void beginOmpOAuth(AppState& st, const std::string& provider) {
    OmpOAuthFlow& f = s_omp.oauth;
    if (f.active) return;
    f = OmpOAuthFlow{};   // reset (keeps codeBuf zeroed)
    f.active = true;
    f.provider = provider;
    f.status = "starting";
    st.rpc.call("agent_provider_begin_oauth", nlohmann::json{{"provider", provider}},
                [&st](const core::RpcResult& r) {
        OmpOAuthFlow& f = s_omp.oauth;
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true)) {
            f.status = "error";
            f.error = !r.ok ? (r.err.empty() ? "sign-in failed to start" : r.err)
                            : optionalJsonString(r.result, "error");
            if (f.error.empty()) f.error = "sign-in failed to start";
            return;
        }
        f.handle = optionalJsonString(r.result, "handle");
        ompOAuthApplySnapshot(r.result);
        if (f.status == "connected") {
            f.active = false;
            st.addLog("[ok] signed in to " + f.provider);
            refreshOmpProvider(st);
        }
    }, 30000);   // bridge waits up to 20s for the first onAuth callback
}

static void pollOmpOAuth(AppState& st) {
    OmpOAuthFlow& f = s_omp.oauth;
    if (!f.active || f.handle.empty() || f.pollInFlight) return;
    if (f.status != "starting" && f.status != "awaiting_auth") return;
    const double now = ImGui::GetTime();
    if ((now - f.lastPoll) < 1.0) return;
    f.lastPoll = now;
    f.pollInFlight = true;
    st.rpc.call("agent_provider_oauth_poll", nlohmann::json{{"handle", f.handle}},
                [&st](const core::RpcResult& r) {
        OmpOAuthFlow& f = s_omp.oauth;
        f.pollInFlight = false;
        if (!r.ok || !r.result.is_object() || !optionalJsonBool(r.result, "ok", true)) {
            // Transient poll failure — keep waiting; the flow itself owns the TTL.
            return;
        }
        ompOAuthApplySnapshot(r.result);
        if (f.status == "connected") {
            f.active = false;
            st.addLog("[ok] signed in to " + f.provider);
            refreshOmpProvider(st);
        } else if (f.status == "error" || f.status == "cancelled") {
            f.active = false;
        }
    }, 10000);
}

static const float* ompStateColor(const std::string& state) {
    if (state == "oauth_connected" || state == "key_set") return ui::palette.statusOk;
    if (state == "error") return ui::palette.statusErr;
    return ui::palette.textDim;   // signed_out / disabled / unknown
}

static const char* ompStateLabel(const OmpProviderRow& r) {
    if (r.state == "oauth_connected") return "OAuth connected";
    if (r.state == "key_set")
        return r.origin == "env" ? "key set (env)" : "API key set";
    if (r.state == "error") return "error";
    if (r.state == "disabled") return "disabled";
    if (r.keyless) return "keyless";
    return "signed out";
}

// Inline status dot + colored label on one line. The bundled UI font has no
// Geometric-Shapes glyphs (U+25CF/U+25CB), so the old literal bullets rendered
// as "?"; draw the dot with the draw-list instead (matches drawStatusDot in
// plm_overview.cpp and the dozens of AddCircleFilled status dots elsewhere).
// filled = solid dot (connected / key set); else a hollow ring (signed out).
static void ompStatusDotText(const char* label, const float col[3], bool filled,
                             const float* labelCol = nullptr) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float sc = ui::uiScale;
    const float fh = ImGui::GetFontSize();
    const float r = fh * 0.30f;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const ImVec2 c(p.x + r + 1.0f * sc, p.y + fh * 0.5f);
    const ImU32 u = ImGui::ColorConvertFloat4ToU32(ImVec4(col[0], col[1], col[2], 1.0f));
    if (filled) dl->AddCircleFilled(c, r, u, 16);
    else        dl->AddCircle(c, r, u, 16, 1.4f * sc);
    ImGui::Dummy(ImVec2(r * 2.0f + 6.0f * sc, fh));  // reserve the dot's width
    ImGui::SameLine(0, 0);
    const ImU32 tu = labelCol
        ? ImGui::ColorConvertFloat4ToU32(ImVec4(labelCol[0], labelCol[1], labelCol[2], 1.0f))
        : u;
    ImGui::PushStyleColor(ImGuiCol_Text, tu);
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
}

// ---- Providers-tab search filtering (provider list + model list) -----------
static std::string toLowerAscii(const std::string& s) {
    std::string out = s;
    for (char& c : out) c = (char)std::tolower((unsigned char)c);
    return out;
}
// needleLower must already be lowercased (case-insensitive substring on label/id).
static bool provMatches(const OmpProviderRow& r, const std::string& needleLower) {
    if (needleLower.empty()) return true;
    return toLowerAscii(r.label).find(needleLower) != std::string::npos ||
           toLowerAscii(r.id).find(needleLower) != std::string::npos;
}
static bool modelMatches(const ui::BridgeModelMeta& m, const std::string& needleLower) {
    if (needleLower.empty()) return true;
    return toLowerAscii(m.name).find(needleLower) != std::string::npos ||
           toLowerAscii(m.id).find(needleLower) != std::string::npos;
}

static void drawOmpProviderDetail(AppState& st, float sc) {
    const OmpProviderRow* row = nullptr;
    for (const auto& r : s_omp.rows)
        if (r.id == s_omp.selectedId) { row = &r; break; }
    if (!row) {
        dimText("Select a provider on the left.");
        return;
    }
    const OmpProviderRow& prov = *row;
    const auto& pal = ui::palette;
    ImGui::PushID(prov.id.c_str());

    fetchOmpProviderDetail(st, prov.id, false);
    const bool detailReady = s_omp.detail.loaded && s_omp.detailFor == prov.id;
    const std::string state = detailReady && !s_omp.detail.state.empty()
                                  ? s_omp.detail.state : prov.state;

    // ---- header: label + state + disable toggle (mapped CAD categories only) ----
    {
        const float badgeSide = ImGui::GetFontSize() * 1.4f;
        ui::drawProviderLogo(ImGui::GetWindowDrawList(), ImGui::GetCursorScreenPos(),
                             badgeSide, prov.id);
        ImGui::Dummy(ImVec2(badgeSide, badgeSide));
        ImGui::SameLine(0, 6.0f * sc);
    }
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
    ImGui::TextUnformatted(prov.label.c_str());
    ImGui::PopStyleColor();
    const float* scol = ompStateColor(state);
    ImGui::SameLine(0, 8.0f * sc);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(scol[0], scol[1], scol[2], 1.0f));
    ImGui::TextUnformatted(ompStateLabel(prov));
    ImGui::PopStyleColor();
    // Disable = CAD's provider-category off switch (composer picker hides the models;
    // credentials stay). Only the bridge providers with a CAD category key get the toggle.
    const char* catKey = nullptr;
    if (prov.id == "anthropic") catKey = "anthropic";
    else if (prov.id == "kimi-code") catKey = "kimi-code";
    else if (prov.id == "moonshot") catKey = "moonshot";
    if (catKey) {
        ImGui::SameLine(0, 16.0f * sc);
        bool disabled = st.settings.disabledProviders.count(catKey) != 0;
        if (ImGui::Checkbox("Disable", &disabled)) {
            if (disabled) st.settings.disabledProviders.insert(catKey);
            else st.settings.disabledProviders.erase(catKey);
            saveSettings(st.settings);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Hide this provider's models from the copilot picker. "
                              "Credentials are kept — re-enable restores.");
    }

    // ---- Sign-in section ----
    settingsGlassSectionBegin("##omp_signin", "Sign-in");
    if (state == "error") {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
        ImGui::TextWrapped("%s", (detailReady && !s_omp.detail.error.empty()
                                      ? s_omp.detail.error : prov.error).c_str());
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry")) refreshOmpProvider(st);
        ImGui::SameLine(0, 8.0f * sc);
    }
    if (state == "oauth_connected") {
        ompStatusDotText("OAuth connected", pal.statusOk, /*filled=*/true);
        if (detailReady) {
            for (const auto& ac : s_omp.detail.accounts) {
                ImGui::PushID(ac.credentialId);
                std::string who = !ac.email.empty() ? ac.email
                                  : (!ac.accountId.empty() ? shortAccountId(ac.accountId)
                                                           : "(account)");
                ImGui::Bullet();
                ImGui::TextUnformatted(who.c_str());
                if (!ac.orgName.empty()) {
                    ImGui::SameLine(0, 6.0f * sc);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                    ImGui::TextUnformatted(ac.orgName.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::PopID();
            }
        }
        if (prov.hasOAuth && !s_omp.oauth.active) {
            if (ImGui::SmallButton("Add account")) beginOmpOAuth(st, prov.id);
            ImGui::SameLine(0, 8.0f * sc);
        }
        if (ImGui::SmallButton("Sign out")) {
            st.rpc.call("agent_provider_logout",
                        nlohmann::json{{"provider", prov.id}},
                        [&st, pid = prov.id](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object() ||
                    !optionalJsonBool(r.result, "ok", true)) {
                    s_omp.lastError = !r.ok ? r.err : optionalJsonString(r.result, "error");
                } else {
                    s_omp.lastError.clear();
                }
                refreshOmpProvider(st);
            });
        }
    } else if (state == "key_set") {
        ompStatusDotText(prov.origin == "env" && !prov.envVar.empty()
                             ? (std::string("key set via ") + prov.envVar).c_str()
                             : "API key set",
                         pal.statusOk, /*filled=*/true);
        if (ImGui::SmallButton("Remove key")) {
            st.rpc.call("agent_provider_remove_key",
                        nlohmann::json{{"provider", prov.id}},
                        [&st](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object() ||
                    !optionalJsonBool(r.result, "ok", true))
                    s_omp.lastError = !r.ok ? r.err : optionalJsonString(r.result, "error");
                refreshOmpProvider(st);
            });
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Remove the stored key from the auth bridge. "
                              "Paste a new key below to replace it.");
    } else if (prov.keyless) {
        dimText("No credential needed — this provider serves models without a key.");
    } else {
        ompStatusDotText("signed out", pal.textDim, /*filled=*/false);
    }

    // OAuth entry point + in-flight flow.
    OmpOAuthFlow& flow = s_omp.oauth;
    const bool flowHere = flow.active && flow.provider == prov.id;
    if (prov.hasOAuth && state != "oauth_connected" && !flow.active) {
        if (primaryButton("Sign in\xe2\x80\xa6", ImVec2(140.0f * sc, 0))) {
            beginOmpOAuth(st, prov.id);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Browser OAuth (PKCE loopback or device code, whichever the "
                              "provider supports). A manual code paste fallback is offered "
                              "when the browser can't be opened.");
    }
    if (flowHere || (!flow.active && flow.provider == prov.id &&
                     (flow.status == "error" || flow.status == "cancelled"))) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (flow.status == "starting") {
            ImGui::TextUnformatted("Starting sign-in\xe2\x80\xa6");
        } else if (flow.status == "awaiting_auth") {
            if (!flow.progress.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.progress.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::TextUnformatted("Waiting for browser approval\xe2\x80\xa6");
            }
            if (!flow.instructions.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.instructions.c_str());
                ImGui::PopStyleColor();
            }
            const std::string& openUrl = !flow.launchUrl.empty() ? flow.launchUrl : flow.url;
            if (!openUrl.empty()) {
                ImGui::TextWrapped("%s", openUrl.c_str());
                if (ImGui::SmallButton("Copy URL")) ImGui::SetClipboardText(openUrl.c_str());
                ImGui::SameLine(0, 6.0f * sc);
                if (ImGui::SmallButton("Open in browser")) {
#if defined(_WIN32)
                    std::system(("start \"\" \"" + openUrl + "\"").c_str());
#elif defined(__APPLE__)
                    std::system(("open '" + openUrl + "'").c_str());
#else
                    std::system(("xdg-open '" + openUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                }
            }
            // Manual fallback: paste the device code / full redirect URL.
            ImGui::SetNextItemWidth(-1);
            ImGui::InputTextWithHint("##omp_oauth_code", "Paste code or redirect URL\xe2\x80\xa6",
                                     flow.codeBuf, sizeof(flow.codeBuf));
            if (ImGui::SmallButton("Submit code") && flow.codeBuf[0]) {
                st.rpc.call("agent_provider_oauth_manual",
                            nlohmann::json{{"handle", flow.handle}, {"code", flow.codeBuf}},
                            [](const core::RpcResult& r) {
                    if (!r.ok || !r.result.is_object() ||
                        !optionalJsonBool(r.result, "ok", true))
                        s_omp.oauth.error = !r.ok ? r.err
                                                  : optionalJsonString(r.result, "error");
                    s_omp.oauth.codeBuf[0] = 0;
                    s_omp.oauth.lastPoll = -1e9;   // pick up the result on the next poll
                });
            }
            ImGui::SameLine(0, 6.0f * sc);
            if (ImGui::SmallButton("Cancel")) {
                st.rpc.call("agent_provider_oauth_cancel",
                            nlohmann::json{{"handle", flow.handle}},
                            [](const core::RpcResult&) {});
                flow.active = false;
                flow.status = "cancelled";
            }
        } else if (flow.status == "error") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
            ImGui::TextWrapped("%s", flow.error.empty() ? "Sign-in failed" : flow.error.c_str());
            ImGui::PopStyleColor();
            if (prov.hasOAuth && ImGui::SmallButton("Try again")) beginOmpOAuth(st, prov.id);
        } else if (flow.status == "cancelled") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted("Sign-in cancelled.");
            ImGui::PopStyleColor();
        }
    }

    // API-key block (inline; validate → set, with an explicit unvalidated save for providers
    // whose wire API has no cheap validation endpoint).
    if (prov.hasApiKey) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (s_omp.keyFor != prov.id) {
            s_omp.keyFor = prov.id;
            s_omp.keyBuf[0] = 0;
            s_omp.keyStatus.clear();
        }
        ImGui::TextUnformatted(state == "key_set" ? "Replace API key" : "Add API key");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##omp_key", "Paste key\xe2\x80\xa6", s_omp.keyBuf,
                                 sizeof(s_omp.keyBuf), ImGuiInputTextFlags_Password);
        const bool haveInput = s_omp.keyBuf[0] != 0;
        if (s_omp.keyBusy) ImGui::BeginDisabled();
        if (ImGui::SmallButton("Validate \x26 add") && haveInput && !s_omp.keyBusy) {
            s_omp.keyBusy = true;
            s_omp.keyStatus.clear();
            const std::string pid = prov.id;
            const std::string key = s_omp.keyBuf;
            st.rpc.call("agent_provider_validate_key",
                        nlohmann::json{{"provider", pid}, {"apiKey", key}},
                        [&st, pid, key](const core::RpcResult& r) {
                bool ok = r.ok && r.result.is_object() &&
                          optionalJsonBool(r.result, "ok", false);
                if (!ok) {
                    s_omp.keyBusy = false;
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = !r.ok ? (r.err.empty() ? "validation failed" : r.err)
                                            : optionalJsonString(r.result, "error");
                    if (s_omp.keyStatus.empty())
                        s_omp.keyStatus = optionalJsonString(r.result, "detail");
                    if (s_omp.keyStatus.empty()) s_omp.keyStatus = "key rejected";
                    return;
                }
                st.rpc.call("agent_provider_set_key",
                            nlohmann::json{{"provider", pid}, {"apiKey", key}},
                            [&st](const core::RpcResult& sr) {
                    s_omp.keyBusy = false;
                    if (!sr.ok || !sr.result.is_object() ||
                        !optionalJsonBool(sr.result, "ok", true)) {
                        s_omp.keyStatusErr = true;
                        s_omp.keyStatus = !sr.ok ? sr.err
                                                 : optionalJsonString(sr.result, "error");
                        return;
                    }
                    s_omp.keyStatusErr = false;
                    s_omp.keyStatus = "key saved";
                    s_omp.keyBuf[0] = 0;
                    refreshOmpProvider(st);
                });
            }, 20000);
        }
        ImGui::SameLine(0, 6.0f * sc);
        if (ImGui::SmallButton("Add unvalidated") && haveInput && !s_omp.keyBusy) {
            s_omp.keyBusy = true;
            s_omp.keyStatus.clear();
            const std::string pid = prov.id;
            const std::string key = s_omp.keyBuf;
            st.rpc.call("agent_provider_set_key",
                        nlohmann::json{{"provider", pid}, {"apiKey", key}},
                        [&st](const core::RpcResult& sr) {
                s_omp.keyBusy = false;
                if (!sr.ok || !sr.result.is_object() ||
                    !optionalJsonBool(sr.result, "ok", true)) {
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = !sr.ok ? sr.err
                                             : optionalJsonString(sr.result, "error");
                    return;
                }
                s_omp.keyStatusErr = false;
                s_omp.keyStatus = "key saved (not validated)";
                s_omp.keyBuf[0] = 0;
                refreshOmpProvider(st);
            });
        }
        if (s_omp.keyBusy) ImGui::EndDisabled();
        if (!s_omp.keyStatus.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ui::Palette::toU32(s_omp.keyStatusErr ? pal.statusErr : pal.statusOk));
            ImGui::TextWrapped("%s", s_omp.keyStatus.c_str());
            ImGui::PopStyleColor();
        }
        if (!prov.consoleUrl.empty()) {
            if (ImGui::SmallButton("Get a key")) {
#if defined(_WIN32)
                std::system(("start \"\" \"" + prov.consoleUrl + "\"").c_str());
#elif defined(__APPLE__)
                std::system(("open '" + prov.consoleUrl + "'").c_str());
#else
                std::system(("xdg-open '" + prov.consoleUrl + "' >/dev/null 2>&1 &").c_str());
#endif
            }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", prov.consoleUrl.c_str());
        }
    }
    settingsGlassSectionEnd();

    // ---- Usage section (OAuth subscription windows + observed-spend notes) ----
    if (detailReady && !s_omp.detail.usage.empty()) {
        settingsGlassSectionBegin("##omp_usage", "Usage");
        for (const auto& w : s_omp.detail.usage) drawUsageWindow(w, sc);
        for (const auto& n : s_omp.detail.usageNotes) dimText(n.c_str());
        settingsGlassSectionEnd();
    }

    // ---- Models section (OMP catalog: capability chips + ctx + $in/$out per Mtok) ----
    {
        int count = 0;
        for (const auto& m : ui::bridgeModelCatalog())
            if (m.provider == prov.id) ++count;
        char modelsTitle[64];
        std::snprintf(modelsTitle, sizeof(modelsTitle), "Models (%d)",
                      count > 0 ? count : prov.modelsAvailable);
        settingsGlassSectionBegin("##omp_models", modelsTitle);

        if (count > 0) {
            if (ImGui::SmallButton("Select all")) {
                for (const auto& m : ui::bridgeModelCatalog())
                    if (m.provider == prov.id) st.settings.hiddenModels.erase(m.id);
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show every catalog model for this provider in the "
                                  "copilot model picker.");
            ImGui::SameLine(0, 6.0f * sc);
            if (ImGui::SmallButton("Clear")) {
                for (const auto& m : ui::bridgeModelCatalog())
                    if (m.provider == prov.id) st.settings.hiddenModels.insert(m.id);
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Hide every catalog model for this provider from the "
                                  "copilot model picker.");
        }

        static char s_modelSearchBuf[64] = "";
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##model_search", "Search models...", s_modelSearchBuf,
                                 sizeof(s_modelSearchBuf));
        const std::string modelNeedle = toLowerAscii(s_modelSearchBuf);

        if (count == 0) {
            dimText(s_omp.available
                        ? "Catalog loading \xe2\x80\x94 or no bundled models for this provider."
                        : "Catalog unavailable.");
        }
        // Same checkbox+name+sublabel row for both the active list and the deprecated group.
        auto drawModelRow = [&](const ui::BridgeModelMeta& m) {
            bool hidden = st.settings.hiddenModels.count(m.id) != 0;
            bool shown = !hidden;
            if (ImGui::Checkbox("##show", &shown)) {
                if (shown) st.settings.hiddenModels.erase(m.id);
                else st.settings.hiddenModels.insert(m.id);
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show/hide in the copilot model picker.");
            ImGui::SameLine(0, 6.0f * sc);
            ImGui::TextUnformatted(m.name.c_str());
            const std::string sub = ui::bridgeModelSublabel(m);
            if (!sub.empty()) {
                const float sw = ImGui::CalcTextSize(sub.c_str()).x;
                const float rx = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x
                                 - sw;
                ImGui::SameLine(rx > ImGui::GetCursorPosX() ? rx : 0, 6.0f * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted(sub.c_str());
                ImGui::PopStyleColor();
            }
            const std::string detail = ui::bridgeModelCostDetail(m);
            if (!detail.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", detail.c_str());
        };

        // Catalog is already provider-sorted newest-first; render active models inline and
        // stash deprecated ones for the collapsed group below (search applies to both).
        std::vector<const ui::BridgeModelMeta*> deprecatedMatches;
        int matchCount = 0;
        for (const auto& m : ui::bridgeModelCatalog()) {
            if (m.provider != prov.id) continue;
            if (!modelMatches(m, modelNeedle)) continue;
            ++matchCount;
            if (m.deprecated) { deprecatedMatches.push_back(&m); continue; }
            ImGui::PushID(m.id.c_str());
            drawModelRow(m);
            ImGui::PopID();
        }
        if (!deprecatedMatches.empty()) {
            char depTitle[48];
            std::snprintf(depTitle, sizeof(depTitle), "Older / deprecated (%d)",
                          (int)deprecatedMatches.size());
            if (ImGui::CollapsingHeader(depTitle)) {
                for (const auto* mp : deprecatedMatches) {
                    ImGui::PushID(mp->id.c_str());
                    drawModelRow(*mp);
                    ImGui::PopID();
                }
            }
        }
        if (count > 0 && matchCount == 0) dimText("No models match your search.");
        settingsGlassSectionEnd();
    }
    ImGui::PopID();
}

// Two-pane provider manager: left = provider list with sign-in state, right = detail
// (Sign-in / Usage / Models). Footer = per-session cost strip. Single-column fallback under
// 640*sc: the list collapses to a combo (design §3.1).
static void drawOmpProviderManager(AppState& st) {
    const float sc = ui::uiScale;
    const auto& pal = ui::palette;
    pollOmpProviderList(st, false);
    pollOmpOAuth(st);
    ui::refreshBridgeModelCatalog(st, false);

    if (!s_omp.available) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("Provider sign-in bridge unavailable in this build \xe2\x80\x94 "
                           "legacy provider settings below.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_omp.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("%s", s_omp.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const float footerH = ImGui::GetFrameHeightWithSpacing() + 4.0f * sc;
    const float paneH = -footerH;
    const float availW = ImGui::GetContentRegionAvail().x;
    const bool narrow = availW < 640.0f * sc;

    static char s_provSearchBuf[64] = "";
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##prov_search", "Search providers...", s_provSearchBuf,
                             sizeof(s_provSearchBuf));
    bool logosOn = ui::providerLogosEnabled();
    if (ImGui::Checkbox("Show provider logos", &logosOn))
        ui::setProviderLogosEnabled(logosOn);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Fetch each provider's real brand icon (cached locally in appdata).\n"
                          "Off = colored letter badges, no network.");
    const std::string provNeedle = toLowerAscii(s_provSearchBuf);
    std::vector<const OmpProviderRow*> filteredRows;
    filteredRows.reserve(s_omp.rows.size());
    for (const auto& r : s_omp.rows)
        if (provMatches(r, provNeedle)) filteredRows.push_back(&r);

    if (narrow) {
        // Collapsed: provider pick as a combo, detail below.
        std::string preview = s_omp.selectedId;
        for (const auto& r : s_omp.rows)
            if (r.id == s_omp.selectedId) { preview = r.label; break; }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::BeginCombo("##omp_prov_pick", preview.c_str())) {
            for (const auto* rp : filteredRows) {
                const OmpProviderRow& r = *rp;
                ImGui::PushID(r.id.c_str());
                const float badgeSide = ImGui::GetFontSize() * 1.1f;
                ui::drawProviderLogo(ImGui::GetWindowDrawList(), ImGui::GetCursorScreenPos(),
                                     badgeSide, r.id);
                ImGui::Dummy(ImVec2(badgeSide, badgeSide));
                ImGui::SameLine(0, 4.0f * sc);
                if (ImGui::Selectable(r.label.c_str(), r.id == s_omp.selectedId))
                    s_omp.selectedId = r.id;
                const float* c = ompStateColor(r.state);
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
                ImGui::TextUnformatted(ompStateLabel(r));
                ImGui::PopStyleColor();
                ImGui::PopID();
            }
            if (filteredRows.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextUnformatted("No providers match your search.");
                ImGui::PopStyleColor();
            }
            ImGui::EndCombo();
        }
    } else {
        // Both halves of the split view share one inner padding — they used to run 8/6 on the
        // left and 0/0 on the right, which made the two panes read as different widgets.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
        ImGui::BeginChild("##omp_prov_list", ImVec2(220.0f * sc, paneH),
                          ImGuiChildFlags_Borders);
        for (const auto* rp : filteredRows) {
            const OmpProviderRow& r = *rp;
            ImGui::PushID(r.id.c_str());
            const bool sel = (r.id == s_omp.selectedId);
            if (ImGui::Selectable("##row", sel, 0,
                                  ImVec2(0, ImGui::GetTextLineHeight() * 2 + 8.0f * sc))) {
                s_omp.selectedId = r.id;
            }
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const ImVec2 mn = ImGui::GetItemRectMin();
            const float fh = ImGui::GetFontSize();
            const float rowH = fh * 2.0f + 8.0f * sc;
            const float badgeSide = fh * 1.4f;
            const float badgeGap = 8.0f * sc;
            const float badgeX = mn.x + 6.0f * sc;
            const float badgeY = mn.y + (rowH - badgeSide) * 0.5f;
            // Center the two-line text block (name + status) on the same row-center as the
            // badge so the badge, name and status read as one vertically-centered unit.
            const float textTop = mn.y + (rowH - (fh * 2.0f + 2.0f * sc)) * 0.5f;
            ui::drawProviderLogo(dl, ImVec2(badgeX, badgeY), badgeSide, r.id);
            const float textX = badgeX + badgeSide + badgeGap;
            dl->AddText(ImVec2(textX, textTop),
                        ui::Palette::toU32(sel ? pal.text : pal.textDim), r.label.c_str());
            const float* c = ompStateColor(r.state);
            const bool bullet = (r.state == "oauth_connected" || r.state == "key_set");
            char statusLine[160];
            std::snprintf(statusLine, sizeof(statusLine), "%s", ompStateLabel(r));
            const float dotR = fh * 0.28f;
            const float sy = textTop + fh + 2.0f * sc;
            const ImU32 cu = ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], 1.0f));
            const ImVec2 dc(textX + dotR, sy + fh * 0.5f);
            if (bullet) dl->AddCircleFilled(dc, dotR, cu, 16);
            else        dl->AddCircle(dc, dotR, cu, 16, 1.4f * sc);
            dl->AddText(ImVec2(textX + dotR * 2.0f + 5.0f * sc, sy), cu, statusLine);
            ImGui::PopID();
        }
        if (filteredRows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextWrapped(s_omp.listInFlight ? "Loading providers\xe2\x80\xa6"
                               : (s_omp.rows.empty() ? "No providers reported by the bridge."
                                                     : "No providers match your search."));
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();
        ImGui::SameLine(0, 8.0f * sc);
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
    ImGui::BeginChild("##omp_prov_detail", ImVec2(0, paneH),
                      ImGuiChildFlags_AlwaysUseWindowPadding);
    drawOmpProviderDetail(st, sc);
    ImGui::EndChild();
    ImGui::PopStyleVar();

    // Footer: per-session cost strip (agent_cost_summary; hides itself when unavailable).
    ui::drawSessionCostStrip(st);
}

// ---- Run benchmark + categories (Settings → Benchmarks) ---------------------
struct BenchUi {
    int source = 1;   // 0=current copilot, 1=xAI grok-4.5, 2=local
    int mode = 0;     // 0=diag, 1=smoke
    bool running = false;
    bool pollInFlight = false;
    double lastPoll = -1e9;
    std::string stage = "idle";
    std::string lastError;
    std::string lastResultPath;
    bool wasRunning = false;
    std::string activeCategory;  // non-empty when last launch was per-category
};
static BenchUi s_bench;

struct BenchCategoryLast {
    std::string when;
    float passRate = -1.f;  // <0 = absent
    std::string detailPath;
};
struct BenchCategory {
    std::string id;
    std::string label;
    std::string description;
    std::vector<std::string> toolsCovered;
    bool runnable = true;
    bool hasLast = false;
    BenchCategoryLast last;
};
struct BenchCategoriesUi {
    std::vector<BenchCategory> cats;
    bool loaded = false;
    bool rpcMissing = false;
    bool pollInFlight = false;
    double lastPoll = -1e9;
    std::string lastError;
};
static BenchCategoriesUi s_cats;

// ---- Model roles (Settings → AI → Roles) -------------------------------------
// RPC: ai_roles_get/set. Degrade read-only when worker offline.
// when the RPC is absent.
//
// Tri-state assignment. Default follows whatever provider+model the chat composer is set
// to (wire form {"mode":"default"} — the resolver sees no provider and falls through to
// the ambient session model). Auto ranks across enabled providers. Fixed pins one.
enum class RoleMode { Default, Auto, Fixed };
static constexpr const char* kRoleModeDefaultLabel = "Default (chat model)";
static constexpr const char* kRoleModeAutoLabel = "Auto (best available)";

// Wire entry → mode: explicit mode wins, else a provider/model pin, else Auto.
static RoleMode roleModeFromWire(const std::string& mode, const std::string& provider,
                                 const std::string& model) {
    if (mode == "default") return RoleMode::Default;
    if (mode == "auto") return RoleMode::Auto;
    if (!provider.empty() || !model.empty()) return RoleMode::Fixed;
    return RoleMode::Auto;
}

struct RoleAssign {
    RoleMode mode = RoleMode::Auto;
    std::string provider;
    std::string model;
    // Per-role reasoning effort: "auto" | "low" | "medium" | "high" (default auto).
    std::string thinking = "auto";
};
struct ModelRolesUi {
    std::map<std::string, RoleAssign> roles;
    bool rolesLoaded = false;
    bool rolesRpcMissing = false;
    bool rolesPollInFlight = false;
    double lastRolesPoll = -1e9;
    std::string lastError;
    bool rolesSaveInFlight = false;
};
static ModelRolesUi s_roles;

static const char* kRoleKeys[] = {
    "advisor", "task", "planning", "vision", "default", "plm", "redaction",
    "sketch2d", "model3d", "memory", "cam", "sim", "ecad", "drawing", "reveng"};
static const char* kRoleLabels[] = {
    "Advisor", "Task", "Planning", "Vision", "Default", "PLM", "Redaction",
    "Sketch 2D", "Model 3D", "Memory", "CAM", "Simulation", "ECAD", "Drawing",
    "Reverse eng"};
// Unique accent per role (RGBA 0–1) — used in Roles + Tools assignment chrome.
static const ImVec4 kRoleColors[] = {
    {0.45f, 0.72f, 0.95f, 1.0f},  // advisor — sky
    {0.95f, 0.62f, 0.35f, 1.0f},  // task — amber
    {0.62f, 0.55f, 0.95f, 1.0f},  // planning — violet
    {0.35f, 0.85f, 0.75f, 1.0f},  // vision — teal
    {0.55f, 0.78f, 0.45f, 1.0f},  // default — green
    {0.40f, 0.65f, 0.85f, 1.0f},  // plm — steel
    {0.90f, 0.40f, 0.40f, 1.0f},  // redaction — red
    {0.85f, 0.70f, 0.35f, 1.0f},  // sketch2d — gold
    {0.35f, 0.70f, 0.90f, 1.0f},  // model3d — cyan
    {0.70f, 0.60f, 0.45f, 1.0f},  // memory — taupe
    {0.90f, 0.50f, 0.55f, 1.0f},  // cam — coral
    {0.55f, 0.85f, 0.55f, 1.0f},  // sim — mint
    {0.75f, 0.55f, 0.90f, 1.0f},  // ecad — orchid
    {0.50f, 0.55f, 0.70f, 1.0f},  // drawing — slate
    {0.85f, 0.55f, 0.30f, 1.0f},  // reveng — orange
};
static const char* kRoleIcons[] = {
    "A", "T", "P", "V", "D", "L", "R",
    "S", "3", "M", "C", "F", "E", "W", "X"};
static constexpr int kRoleCount = 15;

// Colored letter badge for a harness role (Roles table + Tools assignment).
static void drawRoleBadge(int roleIdx, float sc) {
    if (roleIdx < 0 || roleIdx >= kRoleCount) roleIdx = 4;
    const ImVec4& col = kRoleColors[roleIdx];
    const float sz = 18.0f * sc;
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 fill = ImGui::ColorConvertFloat4ToU32(col);
    const ImU32 border = ImGui::ColorConvertFloat4ToU32(
        ImVec4(col.x * 0.55f, col.y * 0.55f, col.z * 0.55f, 1.0f));
    dl->AddCircleFilled(ImVec2(p.x + sz * 0.5f, p.y + sz * 0.5f), sz * 0.48f, fill, 16);
    dl->AddCircle(ImVec2(p.x + sz * 0.5f, p.y + sz * 0.5f), sz * 0.48f, border, 16, 1.2f * sc);
    const char* glyph = kRoleIcons[roleIdx];
    const ImVec2 ts = ImGui::CalcTextSize(glyph);
    dl->AddText(ImVec2(p.x + (sz - ts.x) * 0.5f, p.y + (sz - ts.y) * 0.5f),
                IM_COL32(255, 255, 255, 240), glyph);
    ImGui::Dummy(ImVec2(sz + 4.0f * sc, sz));
}

static std::string benchTierForSource(const Settings& s, int source) {
    if (source == 1) return "xai:grok-4-5";
    if (source == 2) return "local-24k";
    // Current copilot model → gym tier string.
    const int p = s.provider;
    const char* model = s.model[0] ? s.model : "";
    if (p == ui::PROV_XAI) {
        std::string m = model[0] ? model : "grok-4-5";
        // Gym tiers use hyphens (grok-4-5); UI may show grok-4.5.
        for (char& c : m) if (c == '.') c = '-';
        return std::string("xai:") + m;
    }
    if (p == ui::PROV_LOCAL || p == ui::PROV_LLAMACPP) return "local-24k";
    if (p == ui::PROV_CLOUDCOMPAT && model[0]) {
        // Moonshot first-class wire provider when the cloud base is api.moonshot.ai.
        if (std::strstr(s.baseUrl, "moonshot"))
            return std::string("moonshot:") + model;
        // OpenRouter path when base looks like openrouter.
        if (std::strstr(s.baseUrl, "openrouter") || std::strstr(model, "/"))
            return std::string("openrouter:") + model;
    }
    if (p == ui::PROV_ANTHROPIC && model[0])
        return std::string("openrouter:anthropic/") + model;
    if (p == ui::PROV_OPENAI && model[0])
        return std::string("openrouter:openai/") + model;
    if (p == ui::PROV_GEMINI && model[0])
        return std::string("openrouter:google/") + model;
    return "local-24k";
}

static bool benchSourceIsPaid(const Settings& s, int source) {
    if (source == 1) return true;   // xAI grok
    if (source == 2) return false;  // local
    // Current copilot: paid if cloud.
    return ui::providerIsCloud(s.provider);
}

static void pollBenchStatus(AppState& st, bool force, ui::leaderboard::Board* lbRefresh) {
    const double now = ImGui::GetTime();
    if (!force && (now - s_bench.lastPoll) < 1.0) return;
    if (s_bench.pollInFlight) return;
    s_bench.lastPoll = now;
    s_bench.pollInFlight = true;
    st.rpc.call("bench_status", nlohmann::json::object(),
                [lbRefresh](const core::RpcResult& r) {
        s_bench.pollInFlight = false;
        if (!r.ok || !r.result.is_object()) return;
        const bool was = s_bench.running;
        s_bench.running = r.result.value("running", false);
        s_bench.stage = r.result.value("stage", std::string("idle"));
        s_bench.lastError = r.result.value("last_error", std::string());
        if (s_bench.lastError == "null") s_bench.lastError.clear();
        // nlohmann may return empty for null; also accept null type.
        if (r.result.contains("last_error") && r.result["last_error"].is_null())
            s_bench.lastError.clear();
        s_bench.lastResultPath = r.result.value("last_result_path", std::string());
        if (r.result.contains("last_result_path") && r.result["last_result_path"].is_null())
            s_bench.lastResultPath.clear();
        // Transition running → done: auto-refresh leaderboard + categories.
        if (was && !s_bench.running) {
            if (lbRefresh) *lbRefresh = ui::leaderboard::load();
            s_cats.lastPoll = -1e9;  // force category re-poll
            s_cats.loaded = false;
        }
        s_bench.wasRunning = s_bench.running;
    });
}

static void pollBenchCategories(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_cats.loaded && (now - s_cats.lastPoll) < 4.0) return;
    if (s_cats.pollInFlight) return;
    s_cats.lastPoll = now;
    s_cats.pollInFlight = true;
    st.rpc.call("bench_categories", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_cats.pollInFlight = false;
        if (!r.ok) {
            s_cats.rpcMissing = true;
            if (s_cats.lastError.empty())
                s_cats.lastError = r.err.empty() ? "bench_categories unavailable" : r.err;
            s_cats.loaded = true;
            return;
        }
        if (!r.result.is_object()) {
            s_cats.rpcMissing = true;
            s_cats.loaded = true;
            return;
        }
        if (r.result.value("ok", true) == false) {
            s_cats.lastError = r.result.value("reason", std::string("bench_categories failed"));
            s_cats.loaded = true;
            return;
        }
        s_cats.rpcMissing = false;
        s_cats.lastError.clear();
        s_cats.cats.clear();
        auto arr = r.result.value("categories", nlohmann::json::array());
        if (!arr.is_array()) { s_cats.loaded = true; return; }
        for (const auto& c : arr) {
            if (!c.is_object()) continue;
            BenchCategory row;
            row.id = c.value("id", std::string());
            row.label = c.value("label", row.id);
            row.description = c.value("description", std::string());
            row.runnable = c.value("runnable", true);
            auto tools = c.value("tools_covered", nlohmann::json::array());
            if (tools.is_array()) {
                for (const auto& t : tools)
                    if (t.is_string()) row.toolsCovered.push_back(t.get<std::string>());
            }
            if (c.contains("last") && c["last"].is_object() && !c["last"].is_null()) {
                const auto& L = c["last"];
                row.hasLast = true;
                row.last.when = L.value("when", std::string());
                if (L.contains("pass_rate") && L["pass_rate"].is_number())
                    row.last.passRate = (float)L["pass_rate"].get<double>();
                row.last.detailPath = L.value("detail_path", std::string());
            }
            s_cats.cats.push_back(std::move(row));
        }
        s_cats.loaded = true;
    });
}

// Wire provider name used by role/ranking RPCs (matches routing / config conventions).
// PROV_CLOUDCOMPAT is a single slot hosting many services discriminated by base_url, so the
// preset's own base_url picks the wire id (moonshot stays native; every other preset resolves
// to its real OMP-bridge id) exactly the way composer.cpp::agentProviderOverride does. Emitting
// a blanket "openrouter" here assigned roles to a provider the user never picked.
static const char* roleProviderWire(int prov, const std::string& model = {}) {
    if (prov == ui::PROV_CLOUDCOMPAT) {
        for (const auto& p : ui::cloudPresets())
            if (p.id == model) return ui::cloudServiceWireName(ui::cloudServiceKey(p.baseUrl));
        return "openai";  // unknown/custom base_url — generic OpenAI-compatible transport
    }
    switch (prov) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_LOCAL:       return "local";
        case ui::PROV_LLAMACPP:    return "llamacpp";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CURSOR:      return "cursor";
        case ui::PROV_KIMISUB:     return "kimi-code";  // bridge id; "kimi-sub" has no resolver
        default: return "local";
    }
}

static int roleProviderFromWire(const std::string& w) {
    if (w == "anthropic") return ui::PROV_ANTHROPIC;
    if (w == "openai") return ui::PROV_OPENAI;
    if (w == "local") return ui::PROV_LOCAL;
    if (w == "llamacpp") return ui::PROV_LLAMACPP;
    if (w == "gemini") return ui::PROV_GEMINI;
    if (w == "openrouter" || w == "cloud" || w == "cloudcompat" || w == "moonshot" ||
        w == "deepseek" || w == "alibaba-coding-plan" || w == "amazon-bedrock" || w == "azure" ||
        w == "google-antigravity" || w == "aimlapi")
        return ui::PROV_CLOUDCOMPAT;
    if (w == "xai" || w == "xai-sub") return ui::PROV_XAI;
    if (w == "cursor" || w == "cursor-sub") return ui::PROV_CURSOR;
    if (w == "kimi-code" || w == "kimi-sub") return ui::PROV_KIMISUB;  // kimi-sub: legacy config
    return -1;
}

static bool providerIsLoopback(int prov) {
    return prov == ui::PROV_LOCAL || prov == ui::PROV_LLAMACPP;
}

// Flat picker entry for role model dropdowns.
struct RolePickEntry {
    RoleMode mode = RoleMode::Fixed;
    int provider = ui::PROV_LOCAL;
    std::string model;
    std::string label;  // display
};

// includeAuto=false for surfaces with no ranked-auto engine (Eng. Firm): those get
// exactly the Default row plus the concrete models.
static std::vector<RolePickEntry> buildRolePickList(AppState& st, bool localOnly,
                                                    bool includeAuto = true) {
    std::vector<RolePickEntry> out;
    out.push_back(RolePickEntry{RoleMode::Default, 0, "", kRoleModeDefaultLabel});
    if (includeAuto)
        out.push_back(RolePickEntry{RoleMode::Auto, 0, "", kRoleModeAutoLabel});
    // Keep the pick list aligned with the composer dropdown: only providers the user can
    // actually run (API key present, subscription signed in, or local endpoint up).
    ui::refreshModelAvailability(st, false);
    auto add = [&](int prov, const std::string& model, const std::string& label) {
        if (localOnly && !providerIsLoopback(prov)) return;
        if (!ui::providerUsable(st, prov)) return;
        RolePickEntry e;
        e.mode = RoleMode::Fixed;
        e.provider = prov;
        e.model = model;
        e.label = label;
        out.push_back(std::move(e));
    };
    // Cloud catalog (Gemini / Anthropic / OpenAI / xAI / Cursor / Kimi Code), in built-in
    // provider order — but each provider's rows come from its LIVE agent_list_models probe when
    // one succeeded (ui::providerPickerModels), so a model released after this build is
    // role-assignable without a catalog edit. The static catalog is the per-provider fallback.
    std::set<int> emittedProv;
    for (const auto& b : ui::builtinModels()) {
        if (!emittedProv.insert(b.provider).second) continue;
        for (const auto& m : ui::providerPickerModels(st, b.provider)) {
            char buf[256];
            std::snprintf(buf, sizeof(buf), "%s \xc2\xb7 %s",
                          ui::providerLabel(m.provider), m.label.c_str());
            add(m.provider, m.id, buf);
        }
    }
    // Cloud presets: OpenRouter/Alibaba/DeepSeek need cloud key (local or host-shared);
    // Moonshot needs moonshot key (local or host-shared).
    for (const auto& p : ui::cloudPresets()) {
        const char* svc = ui::cloudServiceKey(p.baseUrl);
        if (std::strcmp(svc, "moonshot") == 0) {
            if (!ui::moonshotKeyPresent(st)) continue;
        } else if (!ui::cloudCompatKeyPresent(st)) {
            continue;
        }
        add(ui::PROV_CLOUDCOMPAT, p.id, p.label);
    }
    // Local presets + live-probed local models — only when local is actually in play:
    // redaction/local-only picker, active provider is local, a local probe succeeded, or
    // Offline/ITAR mode. Cloud-only users with no local server see no Local rows.
    const bool includeLocal =
        localOnly ||
        st.settings.offlineMode ||
        providerIsLoopback(st.settings.provider) ||
        st.providerModels[ui::PROV_LOCAL].ok ||
        st.providerModels[ui::PROV_LLAMACPP].ok;
    if (includeLocal) {
        for (const auto& p : ui::localPresets()) {
            // Presets still require a usable local endpoint (or offline intent).
            if (!ui::providerUsable(st, ui::PROV_LOCAL) && !st.settings.offlineMode &&
                !providerIsLoopback(st.settings.provider))
                break;
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
    }
    return out;
}

static void ensureDefaultRoles() {
    for (int i = 0; i < kRoleCount; ++i) {
        if (s_roles.roles.find(kRoleKeys[i]) == s_roles.roles.end()) {
            RoleAssign a;
            a.mode = RoleMode::Auto;
            s_roles.roles[kRoleKeys[i]] = a;
        }
    }
}

static void pollModelRoles(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    if (!force && s_roles.rolesLoaded && (now - s_roles.lastRolesPoll) < 5.0) return;
    if (s_roles.rolesPollInFlight || s_roles.rolesSaveInFlight) return;
    s_roles.lastRolesPoll = now;
    s_roles.rolesPollInFlight = true;
    st.rpc.call("ai_roles_get", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_roles.rolesPollInFlight = false;
        if (!r.ok) {
            s_roles.rolesRpcMissing = true;
            ensureDefaultRoles();
            s_roles.rolesLoaded = true;
            return;
        }
        if (!r.result.is_object()) {
            s_roles.rolesRpcMissing = true;
            ensureDefaultRoles();
            s_roles.rolesLoaded = true;
            return;
        }
        s_roles.rolesRpcMissing = false;
        auto roles = optionalJsonObj(r.result, "roles");
        if (!roles.is_object()) roles = nlohmann::json::object();
        s_roles.roles.clear();
        for (int i = 0; i < kRoleCount; ++i) {
            RoleAssign a;
            a.mode = RoleMode::Auto;
            a.thinking = "auto";
            auto it = roles.find(kRoleKeys[i]);
            if (it != roles.end()) {
                if (it->is_string()) {
                    // A bare string never pins a model (the resolver requires an object
                    // with a provider), so anything but "default" means Auto.
                    a.mode = (it->get<std::string>() == "default") ? RoleMode::Default
                                                                   : RoleMode::Auto;
                } else if (it->is_object()) {
                    a.provider = optionalJsonString(*it, "provider");
                    a.model = optionalJsonString(*it, "model");
                    // mode=default → chat model, mode=auto → ranked pick, provider/model
                    // → fixed pin, nothing recognized → Auto.
                    a.mode = roleModeFromWire(optionalJsonString(*it, "mode"),
                                              a.provider, a.model);
                    // thinking: auto|low|medium|high (default auto).
                    std::string th = optionalJsonString(*it, "thinking");
                    if (th.empty()) th = "auto";
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
            }
            s_roles.roles[kRoleKeys[i]] = std::move(a);
        }
        s_roles.rolesLoaded = true;
    });
}

static void setRoleAssign(AppState& st, const char* roleKey, const RoleAssign& a) {
    if (s_roles.rolesRpcMissing) return;
    s_roles.roles[roleKey] = a;
    nlohmann::json p;
    nlohmann::json roles = nlohmann::json::object();
    // Always object form so thinking can ride alongside model auto/fixed.
    nlohmann::json entry = nlohmann::json::object();
    if (a.mode == RoleMode::Default) {
        entry["mode"] = "default";  // no provider → resolver falls through to chat model
    } else if (a.mode == RoleMode::Auto) {
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
    s_roles.rolesSaveInFlight = true;
    st.rpc.call("ai_roles_set", p, [](const core::RpcResult& r) {
        s_roles.rolesSaveInFlight = false;
        if (!r.ok) {
            s_roles.lastError = r.err.empty() ? "ai_roles_set failed" : r.err;
            return;
        }
        // Prefer server echo when present.
        if (r.result.is_object() && r.result.contains("roles") && r.result["roles"].is_object()) {
            const auto& roles = r.result["roles"];
            for (int i = 0; i < kRoleCount; ++i) {
                auto it = roles.find(kRoleKeys[i]);
                if (it == roles.end()) continue;
                RoleAssign a;
                a.thinking = "auto";
                if (it->is_string()) {
                    a.mode = (it->get<std::string>() == "default") ? RoleMode::Default
                                                                   : RoleMode::Auto;
                } else if (it->is_object()) {
                    a.provider = optionalJsonString(*it, "provider");
                    a.model = optionalJsonString(*it, "model");
                    a.mode = roleModeFromWire(optionalJsonString(*it, "mode"),
                                              a.provider, a.model);
                    std::string th = optionalJsonString(*it, "thinking");
                    if (th.empty()) th = "auto";
                    if (th != "low" && th != "medium" && th != "high" && th != "auto")
                        th = "auto";
                    a.thinking = th;
                }
                s_roles.roles[kRoleKeys[i]] = a;
            }
        }
        s_roles.lastRolesPoll = ImGui::GetTime();
    });
}

static void drawPassRateBar(float rate, float width, float height) {
    // Local copy of leaderboard bar so category rows don't need the full Board.
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float h = height > 0 ? height : 8.0f * sc;
    const float w = width > 0 ? width : ImGui::GetContentRegionAvail().x;
    const float r = std::max(0.f, std::min(1.f, rate));
    ImU32 bg = ui::Palette::toU32(pal.accentSoft);
    ImU32 fg = r >= 0.5f ? ui::Palette::toU32(pal.statusOk)
             : r >= 0.2f ? ui::Palette::toU32(pal.statusWarn)
                         : ui::Palette::toU32(pal.statusErr);
    dl->AddRectFilled(p, ImVec2(p.x + w, p.y + h), bg, 3.0f * sc);
    if (r > 0.001f)
        dl->AddRectFilled(p, ImVec2(p.x + w * r, p.y + h), fg, 3.0f * sc);
    ImGui::Dummy(ImVec2(w, h));
}

static void drawBenchStatusLine(AppState& st, float sc) {
    if (s_bench.running) {
        char msg[256];
        if (!s_bench.activeCategory.empty())
            std::snprintf(msg, sizeof(msg), "Running %s (%s)", s_bench.stage.c_str(),
                          s_bench.activeCategory.c_str());
        else
            std::snprintf(msg, sizeof(msg), "Running %s", s_bench.stage.c_str());
        ui::busyLabel(msg);
    } else if (s_bench.stage == "done") {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
        ImGui::TextUnformatted("Done \xe2\x80\x94 leaderboard refreshed");
        ImGui::PopStyleColor();
        if (!s_bench.lastResultPath.empty()) {
            ImGui::SameLine();
            dimText(s_bench.lastResultPath.c_str());
        }
    } else if (s_bench.stage == "error" && !s_bench.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_bench.lastError.c_str());
        ImGui::PopStyleColor();
    } else {
        dimText("Idle \xe2\x80\x94 pick a category or legacy smoke/diag run.");
    }
    (void)st; (void)sc;
}

static void drawBenchCategories(AppState& st, float sc) {
    pollBenchCategories(st, /*force=*/false);

    if (!s_cats.loaded && s_cats.pollInFlight) {
        dimText("Loading categories\xe2\x80\xa6");
        return;
    }
    if (s_cats.rpcMissing || (!s_cats.loaded && !st.workerStarted)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "Benchmark categories unavailable \xe2\x80\x94 start the worker to load "
            "category runs. The legacy runner below still works.");
        ImGui::PopStyleColor();
        if (!s_cats.lastError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_cats.lastError.c_str());
            ImGui::PopStyleColor();
        }
        return;
    }
    if (s_cats.cats.empty()) {
        dimText("No benchmark categories reported yet.");
        return;
    }

    dimText("One row per tool family. Run queues that suite; history comes from the last cycle.");

    if (ImGui::BeginTable("##bench_cats", 4,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp |
                              ImGuiTableFlags_NoHostExtendY)) {
        ImGui::TableSetupColumn("Category", ImGuiTableColumnFlags_WidthStretch, 2.0f);
        ImGui::TableSetupColumn("Tools", ImGuiTableColumnFlags_WidthFixed, 56.0f * sc);
        ImGui::TableSetupColumn("Last result", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Run", ImGuiTableColumnFlags_WidthFixed, 64.0f * sc);
        ImGui::TableHeadersRow();

        for (const auto& cat : s_cats.cats) {
            ImGui::TableNextRow();
            ImGui::PushID(cat.id.c_str());

            ImGui::TableNextColumn();
            ImGui::TextUnformatted(cat.label.empty() ? cat.id.c_str() : cat.label.c_str());
            if (!cat.description.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", cat.description.c_str());

            ImGui::TableNextColumn();
            {
                char buf[32];
                std::snprintf(buf, sizeof(buf), "%d", (int)cat.toolsCovered.size());
                ImGui::TextUnformatted(buf);
                if (ImGui::IsItemHovered() && !cat.toolsCovered.empty()) {
                    ImGui::BeginTooltip();
                    ImGui::TextUnformatted("Tools covered:");
                    for (const auto& t : cat.toolsCovered)
                        ImGui::BulletText("%s", t.c_str());
                    ImGui::EndTooltip();
                }
            }

            ImGui::TableNextColumn();
            if (!cat.hasLast) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
                ImGui::TextUnformatted("\xe2\x80\x94 never run");
                ImGui::PopStyleColor();
            } else {
                // Compact single-line cell: bar + % only. Long when/detail paths go in
                // the tooltip — dimText uses TextWrapped and was stacking one glyph per
                // line after SameLine inside a stretch column.
                const float rowH = ImGui::GetTextLineHeight();
                if (cat.last.passRate >= 0.f) {
                    const float pctW = 44.0f * sc;
                    const float gap = 6.0f * sc;
                    const float barW = std::max(48.0f * sc,
                        ImGui::GetContentRegionAvail().x - pctW - gap);
                    // Vertically center the thin bar with the % text.
                    const float y0 = ImGui::GetCursorPosY();
                    ImGui::SetCursorPosY(y0 + std::max(0.f, (rowH - 8.0f * sc) * 0.5f));
                    drawPassRateBar(cat.last.passRate, barW, 8.0f * sc);
                    ImGui::SameLine(0, gap);
                    ImGui::SetCursorPosY(y0);
                    char pct[32];
                    std::snprintf(pct, sizeof(pct), "%.0f%%", cat.last.passRate * 100.f);
                    ImGui::TextUnformatted(pct);
                } else {
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.accent));
                    ImGui::TextUnformatted("ran");
                    ImGui::PopStyleColor();
                }
                // Tooltip on the % (or "ran") label — bar Dummy has no hover id.
                if (ImGui::IsItemHovered()) {
                    ImGui::BeginTooltip();
                    if (cat.last.passRate >= 0.f)
                        ImGui::Text("Pass rate: %.0f%%", cat.last.passRate * 100.f);
                    if (!cat.last.when.empty())
                        ImGui::Text("When: %s", cat.last.when.c_str());
                    if (!cat.last.detailPath.empty())
                        ImGui::Text("Detail: %s", cat.last.detailPath.c_str());
                    if (cat.last.when.empty() && cat.last.detailPath.empty() &&
                        cat.last.passRate < 0.f)
                        ImGui::TextUnformatted("Completed (no detail path).");
                    ImGui::EndTooltip();
                }
            }

            ImGui::TableNextColumn();
            {
                const bool canRun = cat.runnable && !s_bench.running && st.workerStarted;
                if (!canRun) ImGui::BeginDisabled();
                if (ImGui::SmallButton("Run")) {
                    nlohmann::json p;
                    p["category"] = cat.id;
                    s_bench.running = true;
                    s_bench.stage = "starting";
                    s_bench.lastError.clear();
                    s_bench.lastPoll = -1e9;
                    s_bench.activeCategory = cat.id;
                    st.rpc.call("bench_run", p, [](const core::RpcResult& r) {
                        if (!r.ok) {
                            s_bench.running = false;
                            s_bench.stage = "error";
                            s_bench.lastError = r.err.empty() ? "bench_run failed" : r.err;
                        } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                            s_bench.running = false;
                            s_bench.stage = "error";
                            s_bench.lastError = r.result.value("reason",
                                std::string("bench_run failed"));
                        }
                        s_bench.lastPoll = -1e9;
                    });
                }
                if (!canRun) ImGui::EndDisabled();
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
                    if (s_bench.running)
                        ImGui::SetTooltip("A benchmark is already running.");
                    else if (!st.workerStarted)
                        ImGui::SetTooltip("Worker not ready yet.");
                    else if (!cat.runnable)
                        ImGui::SetTooltip("This category is not runnable.");
                    else
                        ImGui::SetTooltip("Run category '%s' via bench_run.", cat.id.c_str());
                }
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    drawBenchStatusLine(st, sc);
}

static void drawBenchRunner(AppState& st, float sc, ui::leaderboard::Board& lb,
                            double& lbLast) {
    ImGui::Dummy(ImVec2(0, 6.0f * sc));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    ImGui::TextUnformatted("Legacy quick run");
    dimText("Spawns the gym CLI detached on the worker (diag / C1 smoke). Prefer the "
            "category rows above for full tool-family coverage. On finish rebuilds "
            "benchmarks/leaderboard/leaderboard.json and refreshes the table.");

    static const char* kSources[] = {
        "Current copilot model",
        "xAI grok-4.5",
        "Local models (local-24k)",
    };
    static const char* kModes[] = {
        "Quick diag",
        "C1 smoke",
    };
    ImGui::SetNextItemWidth(220.0f * sc);
    ImGui::Combo("##bench_src", &s_bench.source, kSources, 3);
    ImGui::SameLine();
    ImGui::SetNextItemWidth(140.0f * sc);
    ImGui::Combo("##bench_mode", &s_bench.mode, kModes, 2);

    if (benchSourceIsPaid(st.settings, s_bench.source)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("Uses your API credits / subscription for the selected model.");
        ImGui::PopStyleColor();
    }

    const bool canRun = !s_bench.running && st.workerStarted;
    if (!canRun) ImGui::BeginDisabled();
    if (ImGui::Button("Run benchmark", ImVec2(160.0f * sc, 0))) {
        const std::string tier = benchTierForSource(st.settings, s_bench.source);
        const char* mode = (s_bench.mode == 0) ? "diag" : "smoke";
        nlohmann::json p;
        p["mode"] = mode;
        p["tiers"] = nlohmann::json::array({tier});
        s_bench.running = true;
        s_bench.stage = "starting";
        s_bench.lastError.clear();
        s_bench.lastPoll = -1e9;
        s_bench.activeCategory.clear();
        st.rpc.call("bench_run", p, [](const core::RpcResult& r) {
            if (!r.ok) {
                s_bench.running = false;
                s_bench.stage = "error";
                s_bench.lastError = r.err.empty() ? "bench_run failed" : r.err;
            } else if (r.result.is_object() && r.result.value("ok", true) == false) {
                s_bench.running = false;
                s_bench.stage = "error";
                s_bench.lastError = r.result.value("reason", std::string("bench_run failed"));
            }
            s_bench.lastPoll = -1e9;
        });
    }
    if (!canRun) ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)) {
        if (s_bench.running)
            ImGui::SetTooltip("A benchmark is already running.");
        else if (!st.workerStarted)
            ImGui::SetTooltip("Worker not ready yet.");
        else
            ImGui::SetTooltip("Start a detached gym CLI run for the selected model.");
    }

    // Status line + spinner while running / after finish.
    pollBenchStatus(st, s_bench.running, &lb);
    ImGui::SameLine();
    drawBenchStatusLine(st, sc);
    if (s_bench.running)
        lbLast = -1e9;
}

static void drawModelRolesSection(AppState& st, float sc) {
    pollModelRoles(st, /*force=*/false);
    ensureDefaultRoles();

    dimText("Per-role model + thinking effort. Default follows the model chosen in the chat "
            "composer; Auto picks the best available within enabled providers; or pin a "
            "specific model. Thinking Auto lets the classifier (Default role) or dispatcher "
            "(subagents) choose. Redaction is hard-locked to local (loopback) providers.");

    if (s_roles.rolesRpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("ai_roles_* RPC not available \xe2\x80\x94 showing defaults (read-only).");
        ImGui::PopStyleColor();
    }
    if (!s_roles.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_roles.lastError.c_str());
        ImGui::PopStyleColor();
    }

    // Build picker once per frame (cheap; catalogs are small).
    const auto allPicks = buildRolePickList(st, /*localOnly=*/false);
    const auto localPicks = buildRolePickList(st, /*localOnly=*/true);

    if (ImGui::BeginTable("##model_roles", 3,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Role", ImGuiTableColumnFlags_WidthStretch, 1.0f);
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Thinking", ImGuiTableColumnFlags_WidthFixed, 110.0f * sc);
        ImGui::TableHeadersRow();

        for (int i = 0; i < kRoleCount; ++i) {
            const char* key = kRoleKeys[i];
            const bool isRedaction = (std::strcmp(key, "redaction") == 0);
            RoleAssign& a = s_roles.roles[key];
            const auto& picks = isRedaction ? localPicks : allPicks;

            ImGui::TableNextRow();
            ImGui::PushID(key);

            ImGui::TableNextColumn();
            ImGui::AlignTextToFramePadding();
            drawRoleBadge(i, sc);
            ImGui::SameLine(0.0f, 6.0f * sc);
            ImGui::TextUnformatted(kRoleLabels[i]);
            // Purpose tooltip on every role (vision has the full vision-review description).
            if (std::strcmp(key, "advisor") == 0) {
                helpMarker("Steering / Q&A advisor that guides agents with short notes without doing the work itself.");
            } else if (std::strcmp(key, "task") == 0) {
                helpMarker("Leaf worker/builder that executes CAD ops, edits, and concrete subagent tasks.");
            } else if (std::strcmp(key, "planning") == 0) {
                helpMarker("Orchestrator / plan-mode model that drafts multi-step build plans and coordinates work.");
            } else if (std::strcmp(key, "vision") == 0) {
                helpMarker("Translates renderings/screenshots for the worker models, checks and verifies their "
                           "work/changes, and guides them through the visual modeling steps \xe2\x80\x94 while "
                           "offloading the bulk of vision work to cheaper models (e.g. Grok) to save premium-vision tokens.");
            } else if (std::strcmp(key, "default") == 0) {
                helpMarker("Main copilot agent loop (agent / swarm / ask modes) when no more specific role applies.");
            } else if (std::strcmp(key, "plm") == 0) {
                helpMarker("Product lifecycle tasks \xe2\x80\x94 releases, document control, versioning, and PLM workflows.");
            } else if (std::strcmp(key, "redaction") == 0) {
                helpMarker("Local-only model that redacts sensitive data before anything leaves the machine.");
            } else if (std::strcmp(key, "sketch2d") == 0) {
                helpMarker("2D specialist: sketches, ECAD drawings, and other planar work. "
                           "Does not build 3D solids — model3d delegates sketch jobs here.");
            } else if (std::strcmp(key, "model3d") == 0) {
                helpMarker("3D feature planner/builder. Plans the solid feature tree, delegates "
                           "profiles to sketch2d, places them on the right plane/face, and "
                           "verifies orientation with vision before committing.");
            } else if (std::strcmp(key, "memory") == 0) {
                helpMarker("Project memory distiller — writes durable decisions/notes to "
                           ".plm/memory.db. Does not design geometry.");
            } else if (std::strcmp(key, "cam") == 0) {
                helpMarker("CAM / toolpath specialist — stock, setups, operations, post.");
            } else if (std::strcmp(key, "sim") == 0) {
                helpMarker("Simulation specialist — FEA, CFD, motion.");
            } else if (std::strcmp(key, "ecad") == 0) {
                helpMarker("Electronics specialist — schematics, PCB, ERC/DRC.");
            } else if (std::strcmp(key, "drawing") == 0) {
                helpMarker("2D drawing / detailing specialist.");
            } else if (std::strcmp(key, "reveng") == 0) {
                helpMarker("Reverse-engineering specialist — scans, photos, reconstruction.");
            }
            if (isRedaction) {
                ImGui::SameLine(0.0f, 6.0f * sc);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
                ImGui::TextUnformatted("[local]");
                ImGui::PopStyleColor();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Local only \xe2\x80\x94 ITAR redaction stage must not leave the machine.\n"
                        "Hard-locked to loopback providers; cloud picks are refused.");
            }

            ImGui::TableNextColumn();
            // Selection index: row 0 = Default, row 1 = Auto, the rest concrete models.
            // sel < 0 means a pinned model that is not in the current pick list.
            int sel = (a.mode == RoleMode::Default) ? 0 : 1;
            if (a.mode == RoleMode::Fixed) {
                sel = -1;
                const int wantProv = roleProviderFromWire(a.provider);
                for (int pi = 0; pi < (int)picks.size(); ++pi) {
                    if (picks[pi].mode != RoleMode::Fixed) continue;
                    if (picks[pi].model == a.model &&
                        (wantProv < 0 || picks[pi].provider == wantProv ||
                         roleProviderWire(picks[pi].provider, picks[pi].model) == a.provider)) {
                        sel = pi;
                        break;
                    }
                }
            }

            // Preview label. An unmatched pin still shows its raw wire value.
            std::string preview;
            if (a.mode == RoleMode::Default) {
                preview = kRoleModeDefaultLabel;
            } else if (a.mode == RoleMode::Auto) {
                preview = kRoleModeAutoLabel;
            } else if (sel >= 0) {
                preview = picks[sel].label;
            } else {
                preview = a.provider + " \xc2\xb7 " + a.model;
                if (preview == " \xc2\xb7 ") preview = a.model.empty() ? kRoleModeAutoLabel
                                                                       : a.model;
            }

            const bool ro = s_roles.rolesRpcMissing || s_roles.rolesSaveInFlight;
            if (ro) ImGui::BeginDisabled();
            ImGui::SetNextItemWidth(-1);
            if (ImGui::BeginCombo("##role_pick", preview.c_str(), ImGuiComboFlags_HeightLarge)) {
                // Type-to-filter — list is already filtered to keyed/signed-in providers.
                static char s_roleFilt[64] = "";
                if (ImGui::IsWindowAppearing()) {
                    s_roleFilt[0] = 0;
                    ImGui::SetKeyboardFocusHere();
                }
                ImGui::SetNextItemWidth(-1);
                ImGui::InputTextWithHint("##role_filt", "Type to filter...",
                                         s_roleFilt, sizeof(s_roleFilt));
                auto lc = [](std::string s) {
                    for (char& c : s) c = (char)std::tolower((unsigned char)c);
                    return s;
                };
                const std::string fq = lc(s_roleFilt);
                for (int pi = 0; pi < (int)picks.size(); ++pi) {
                    if (!fq.empty()) {
                        const std::string hay = lc(picks[pi].label + " " + picks[pi].model);
                        if (hay.find(fq) == std::string::npos) continue;
                    }
                    const bool selected = (pi == sel);
                    if (ImGui::Selectable(picks[pi].label.c_str(), selected)) {
                        RoleAssign next = a;  // preserve thinking
                        next.mode = picks[pi].mode;
                        if (picks[pi].mode == RoleMode::Fixed) {
                            next.provider = roleProviderWire(picks[pi].provider, picks[pi].model);
                            next.model = picks[pi].model;
                        } else {
                            next.provider.clear();
                            next.model.clear();
                        }
                        // Hard-enforce redaction local-only client-side too. Default is
                        // refused as well: the chat model is usually a cloud provider.
                        if (isRedaction && next.mode == RoleMode::Default) {
                            s_roles.lastError =
                                "Redaction role cannot follow the chat model \xe2\x80\x94 "
                                "pick Auto or a loopback model.";
                        } else if (isRedaction && next.mode == RoleMode::Fixed &&
                                   !providerIsLoopback(picks[pi].provider)) {
                            s_roles.lastError =
                                "Redaction role refuses non-loopback providers.";
                        } else {
                            s_roles.lastError.clear();
                            setRoleAssign(st, key, next);
                        }
                    }
                    if (selected) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
            if (ro) ImGui::EndDisabled();

            // Thinking effort combo (Auto / Low / Medium / High).
            ImGui::TableNextColumn();
            static const char* kThinkLabels[] = {"Auto", "Low", "Medium", "High"};
            static const char* kThinkValues[] = {"auto", "low", "medium", "high"};
            int thinkSel = 0;
            for (int ti = 0; ti < 4; ++ti) {
                if (a.thinking == kThinkValues[ti]) { thinkSel = ti; break; }
            }
            if (ro) ImGui::BeginDisabled();
            ImGui::SetNextItemWidth(-1);
            if (ImGui::BeginCombo("##role_think", kThinkLabels[thinkSel])) {
                for (int ti = 0; ti < 4; ++ti) {
                    const bool selT = (ti == thinkSel);
                    if (ImGui::Selectable(kThinkLabels[ti], selT)) {
                        RoleAssign next = a;
                        next.thinking = kThinkValues[ti];
                        setRoleAssign(st, key, next);
                    }
                    if (selT) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
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

            // Redaction ITAR note lives on the Role [local] badge tooltip — never
            // TextWrapped inside the fixed-width Thinking column (that blew out the row).

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

}

// Copilot mode → default harness structure + one-line description. Single source of
// truth for Settings → AI → Harness organization (index matches CopilotMode).
struct HarnessModeInfo {
    const char* label;       // Agent | Plan | Swarm | Edit | Ask | Eng. Firm
    const char* structure;   // default execution structure
    const char* description; // one-line what it does
};
static const HarnessModeInfo kHarnessModes[] = {
    {"Agent",    "auto",
     "Solo for simple, plan/subagent for standard, DAG/company for complex."},
    {"Plan",     "plan-only",
     "Drafts a plan only; no mutation until you approve."},
    {"Swarm",    "multi-agent parallel",
     "Forces a multi-agent / corporate mission with parallel specialists."},
    {"Edit",     "scoped single-doc edit",
     "Changes the open part (scoped to selection when set)."},
    {"Ask",      "read-only Q&A",
     "Questions about the part; nothing changes."},
    {"Eng. Firm","multi-agent + contractor firms",
     "Specialist roles + contractor firms bid on tasks under a $ budget."},
};
static constexpr int kHarnessModeCount =
    (int)(sizeof(kHarnessModes) / sizeof(kHarnessModes[0]));

// Settings → AI → Harness: mode grid + full quick-settings mirror (session knobs).
static void drawHarnessOrganizationSection(AppState& st, float sc);
bool        drawHarnessSessionKnobs(AppState& st, float sc);
static void drawMemoryLearningSection(AppState& st, float sc);

// ---- Eng. Firm model assignments (config.json eng_firm.models) ----------------
// Top-level roles + 8 disciplines. Two states only — there is no ranked-auto engine here:
// Default writes {"mode":"default"} (empty provider ⇒ the resolver falls through to the
// ambient chat model), otherwise a pinned provider+model. Merge-overlay load/save never
// clobbers unrelated keys.
struct EngFirmAssign {
    bool isDefault = true;
    std::string provider;
    std::string model;
    std::string thinking = "medium";  // low|medium|high (default state omits thinking key)
};
struct EngFirmModelsUi {
    std::map<std::string, EngFirmAssign> top;   // mission_planner|advisor|lead|subcontractor
    std::map<std::string, EngFirmAssign> disc;  // 8 discipline keys
    bool loaded = false;
};
static EngFirmModelsUi s_efModels;

static const char* kEfTopKeys[] = {
    "mission_planner", "advisor", "lead", "subcontractor"};
static const char* kEfTopLabels[] = {
    "Mission planner", "Advisor", "Lead (prime contractor)", "Subcontractor (default)"};
static constexpr int kEfTopCount = 4;

static const char* kEfDiscKeys[] = {
    "mechanical", "electrical", "structural", "thermal",
    "manufacturing", "drafting", "sourcing", "generalist"};
static const char* kEfDiscLabels[] = {
    "Mechanical", "Electrical", "Structural", "Thermal",
    "Manufacturing", "Drafting", "Sourcing", "Generalist"};
static constexpr int kEfDiscCount = 8;

static EngFirmAssign parseEngFirmAssign(const nlohmann::json& j) {
    EngFirmAssign a;
    if (!j.is_object()) return a;
    a.provider = j.value("provider", std::string());
    a.model = j.value("model", std::string());
    std::string th = j.value("thinking", std::string());
    if (th != "low" && th != "medium" && th != "high") th = "medium";
    a.thinking = th.empty() ? "medium" : th;
    // Explicit {"mode":"default"} and the legacy empty provider+model form both mean
    // "follow the chat model".
    a.isDefault = j.value("mode", std::string()) == "default" ||
                  (a.provider.empty() && a.model.empty());
    return a;
}

static void loadEngFirmModelsUi() {
    s_efModels.top.clear();
    s_efModels.disc.clear();
    for (int i = 0; i < kEfTopCount; ++i) s_efModels.top[kEfTopKeys[i]] = EngFirmAssign{};
    for (int i = 0; i < kEfDiscCount; ++i) s_efModels.disc[kEfDiscKeys[i]] = EngFirmAssign{};
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object() && j.contains("eng_firm") && j["eng_firm"].is_object()) {
                const auto& ef = j["eng_firm"];
                if (ef.contains("models") && ef["models"].is_object()) {
                    const auto& m = ef["models"];
                    for (int i = 0; i < kEfTopCount; ++i) {
                        if (m.contains(kEfTopKeys[i]))
                            s_efModels.top[kEfTopKeys[i]] = parseEngFirmAssign(m[kEfTopKeys[i]]);
                    }
                    if (m.contains("disciplines") && m["disciplines"].is_object()) {
                        const auto& d = m["disciplines"];
                        for (int i = 0; i < kEfDiscCount; ++i) {
                            if (d.contains(kEfDiscKeys[i]))
                                s_efModels.disc[kEfDiscKeys[i]] =
                                    parseEngFirmAssign(d[kEfDiscKeys[i]]);
                        }
                    }
                }
            }
        }
    } catch (...) {}
    s_efModels.loaded = true;
}

static void saveEngFirmModelsUi() {
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("eng_firm") || !j["eng_firm"].is_object())
            j["eng_firm"] = nlohmann::json::object();
        nlohmann::json models = nlohmann::json::object();
        // Preserve unknown sibling keys under eng_firm.models (and disciplines).
        if (j["eng_firm"].contains("models") && j["eng_firm"]["models"].is_object())
            models = j["eng_firm"]["models"];

        auto writeEntry = [](nlohmann::json& parent, const char* key, const EngFirmAssign& a) {
            if (a.isDefault || a.model.empty()) {
                // Explicit default state — the row stays in config.json instead of
                // silently vanishing. Empty provider ⇒ ambient chat model.
                nlohmann::json e = nlohmann::json::object();
                e["mode"] = "default";
                parent[key] = std::move(e);
                return;
            }
            nlohmann::json e = nlohmann::json::object();
            e["provider"] = a.provider;
            e["model"] = a.model;
            if (a.thinking == "low" || a.thinking == "medium" || a.thinking == "high")
                e["thinking"] = a.thinking;
            parent[key] = std::move(e);
        };

        for (int i = 0; i < kEfTopCount; ++i)
            writeEntry(models, kEfTopKeys[i], s_efModels.top[kEfTopKeys[i]]);

        nlohmann::json discs = nlohmann::json::object();
        if (models.contains("disciplines") && models["disciplines"].is_object())
            discs = models["disciplines"];
        for (int i = 0; i < kEfDiscCount; ++i)
            writeEntry(discs, kEfDiscKeys[i], s_efModels.disc[kEfDiscKeys[i]]);
        if (discs.empty())
            models.erase("disciplines");
        else
            models["disciplines"] = std::move(discs);

        if (models.empty())
            j["eng_firm"].erase("models");
        else
            j["eng_firm"]["models"] = std::move(models);
        if (j["eng_firm"].empty())
            j.erase("eng_firm");
    });
}

static void drawEngFirmAssignRow(AppState& st, const char* id, const char* label,
                                 EngFirmAssign& a, const std::vector<RolePickEntry>& picks,
                                 float sc) {
    (void)sc;
    ImGui::TableNextRow();
    ImGui::PushID(id);

    ImGui::TableNextColumn();
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(label);

    ImGui::TableNextColumn();
    int sel = 0;  // row 0 = Default (chat model); the rest are concrete models
    if (!a.isDefault) {
        sel = -1;
        const int wantProv = roleProviderFromWire(a.provider);
        for (int pi = 0; pi < (int)picks.size(); ++pi) {
            if (picks[pi].mode != RoleMode::Fixed) continue;
            if (picks[pi].model == a.model &&
                (wantProv < 0 || picks[pi].provider == wantProv ||
                 roleProviderWire(picks[pi].provider, picks[pi].model) == a.provider)) {
                sel = pi;
                break;
            }
        }
    }
    std::string preview;
    if (a.isDefault) {
        preview = kRoleModeDefaultLabel;
    } else if (sel >= 0) {
        preview = picks[sel].label;
    } else {
        preview = a.provider + " \xc2\xb7 " + a.model;
        if (preview == " \xc2\xb7 ")
            preview = a.model.empty() ? kRoleModeDefaultLabel : a.model;
    }
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##ef_pick", preview.c_str(), ImGuiComboFlags_HeightLarge)) {
        static char s_efFilt[64] = "";
        if (ImGui::IsWindowAppearing()) {
            s_efFilt[0] = 0;
            ImGui::SetKeyboardFocusHere();
        }
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##ef_filt", "Type to filter...",
                                 s_efFilt, sizeof(s_efFilt));
        auto lc = [](std::string s) {
            for (char& c : s) c = (char)std::tolower((unsigned char)c);
            return s;
        };
        const std::string fq = lc(s_efFilt);
        for (int pi = 0; pi < (int)picks.size(); ++pi) {
            if (!fq.empty()) {
                const std::string hay = lc(picks[pi].label + " " + picks[pi].model);
                if (hay.find(fq) == std::string::npos) continue;
            }
            const bool selected = (pi == sel);
            if (ImGui::Selectable(picks[pi].label.c_str(), selected)) {
                if (picks[pi].mode == RoleMode::Fixed) {
                    a.isDefault = false;
                    a.provider = roleProviderWire(picks[pi].provider, picks[pi].model);
                    a.model = picks[pi].model;
                    if (a.thinking != "low" && a.thinking != "medium" && a.thinking != "high")
                        a.thinking = "medium";
                } else {
                    a.isDefault = true;
                    a.provider.clear();
                    a.model.clear();
                }
                saveEngFirmModelsUi();
            }
            if (selected) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }

    ImGui::TableNextColumn();
    static const char* kThinkLabels[] = {"Low", "Medium", "High"};
    static const char* kThinkValues[] = {"low", "medium", "high"};
    int thinkSel = 1;  // medium default
    for (int ti = 0; ti < 3; ++ti) {
        if (a.thinking == kThinkValues[ti]) { thinkSel = ti; break; }
    }
    if (a.isDefault) ImGui::BeginDisabled();
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##ef_think", kThinkLabels[thinkSel])) {
        for (int ti = 0; ti < 3; ++ti) {
            const bool selT = (ti == thinkSel);
            if (ImGui::Selectable(kThinkLabels[ti], selT)) {
                a.thinking = kThinkValues[ti];
                saveEngFirmModelsUi();
            }
            if (selT) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }
    if (a.isDefault) ImGui::EndDisabled();

    ImGui::PopID();
}

static void drawEngFirmModelAssignments(AppState& st, float sc) {
    if (!s_efModels.loaded) loadEngFirmModelsUi();

    dimText("Default (chat model) rows follow whatever model the chat composer is set to "
            "\xe2\x80\x94 there is no ranked auto-pick here. Eng. Firm runs resolve: "
            "discipline -> lead/subcontractor -> chat model.");

    const auto picks = buildRolePickList(st, /*localOnly=*/false, /*includeAuto=*/false);

    if (ImGui::BeginTable("##ef_models", 3,
                          ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                              ImGuiTableFlags_SizingStretchProp)) {
        ImGui::TableSetupColumn("Role", ImGuiTableColumnFlags_WidthStretch, 1.2f);
        ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
        ImGui::TableSetupColumn("Thinking", ImGuiTableColumnFlags_WidthFixed, 110.0f * sc);
        ImGui::TableHeadersRow();

        for (int i = 0; i < kEfTopCount; ++i)
            drawEngFirmAssignRow(st, kEfTopKeys[i], kEfTopLabels[i],
                                s_efModels.top[kEfTopKeys[i]], picks, sc);

        // Lightweight separator label row for disciplines.
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        dimText("Disciplines");
        ImGui::TableNextColumn();
        ImGui::TableNextColumn();

        for (int i = 0; i < kEfDiscCount; ++i)
            drawEngFirmAssignRow(st, kEfDiscKeys[i], kEfDiscLabels[i],
                                s_efModels.disc[kEfDiscKeys[i]], picks, sc);

        ImGui::EndTable();
    }
}

static void drawHarnessOrganizationSection(AppState& st, float sc) {
    // Two-column mode chips: active mode accented, others secondary. Saves vertical
    // space vs the old 6-line stack (screenshot ask).
    dimText("Composer mode picks the org structure. Switch mode from the copilot composer.");
    int active = st.settings.copilotMode;
    if (active < 0 || active >= kHarnessModeCount) active = 0;

    ImGui::Dummy(ImVec2(0, 4.0f * sc));
    if (ImGui::BeginTable("##harness_modes", 2,
                          ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX)) {
        for (int i = 0; i < kHarnessModeCount; ++i) {
            const auto& m = kHarnessModes[i];
            ImGui::TableNextColumn();
            char line[192];
            std::snprintf(line, sizeof(line), "%s  \xc2\xb7  %s%s",
                          m.label, m.structure, (i == active) ? "  (active)" : "");
            if (i == active) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                           ui::palette.accent[2], 1.0f));
                ImGui::TextUnformatted(line);
                ImGui::PopStyleColor();
            } else {
                ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
                ImGui::TextUnformatted(line);
                ImGui::PopStyleColor();
            }
            if (ImGui::IsItemHovered() && m.description && m.description[0])
                ImGui::SetTooltip("%s", m.description);
        }
        ImGui::EndTable();
    }
}

// Settings → AI → Harness: full mirror of copilot Quick settings + extras that
// only live in the modal (role adoption, company auto-route, Eng. Firm budget,
// privacy). Laid out in a 2-column grid to save vertical space.
bool drawHarnessSessionKnobs(AppState& st, float sc) {
    Settings& settings = st.settings;
    bool changed = false;
    static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};

    auto prefCombo = [&](const char* label, const char* id, int* pref, const char* help) {
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted(label);
        if (help && help[0]) {
            ImGui::SameLine();
            helpMarker(help);
        }
        ImGui::SetNextItemWidth(-1.0f);
        int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
        if (ImGui::Combo(id, &v, kPrefLabels, 4)) {
            *pref = v;
            if (pref == &settings.planPreference)
                settings.planMode = (v != 0);
            changed = true;
        }
    };

    // ---- Preferences (3 combos) + flags in a 2-col grid ----
    if (ImGui::BeginTable("##harness_prefs", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        prefCombo("Plan preference", "##h_plan_pref", &settings.planPreference,
                  "Off = never plan first (default — agent tools directly). "
                  "Option = model may plan. "
                  "Encourage = plan by default. "
                  "Require = always draft a plan before building.");
        ImGui::TableNextColumn();
        prefCombo("Subagent preference", "##h_sub_pref", &settings.subagentPreference,
                  "How freely the main agent may spawn subagents. "
                  "Off disables nesting; Require pushes multi-agent splits.");
        ImGui::TableNextColumn();
        prefCombo("Advisor preference", "##h_adv_pref", &settings.advisorPreference,
                  "Off = no advisor. Option = only when the model asks. "
                  "Encourage = stronger guidance tone when proactive fires. "
                  "Require = more frequent checks when proactive is on.");
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Role adoption");
            ImGui::SameLine();
            helpMarker(
                "Supervised (default): only the main session may adopt_role "
                "(switch tools+prompt). Subagents may still delegate() children "
                "with other roles, but must request_role_change to adopt themselves. "
                "Free: subagents may adopt_role too.");
            ImGui::SetNextItemWidth(-1.0f);
            int ra = (settings.roleAdoption != 0) ? 1 : 0;
            const char* kAdopt[] = {"Supervised", "Free"};
            if (ImGui::Combo("##h_role_adopt", &ra, kAdopt, 2)) {
                settings.roleAdoption = ra;
                changed = true;
            }
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 6.0f * sc));

    // ---- Checkboxes in 2 columns (mirrors quick-settings flags) ----
    if (ImGui::BeginTable("##harness_flags", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Proactive advisor", &settings.proactiveAdvisor)) changed = true;
        helpMarker("Opt-in mid-run guidance on failure streaks and thrash. "
                   "Cheap deterministic notes by default; at most one short "
                   "advisor-model call on severe streaks (encourage/require). "
                   "Off = no proactive injects (pull request_advice still works).");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Company structure auto-route", &settings.corporateMode)) changed = true;
        helpMarker("When on, complex cross-discipline requests may route as "
                   "company (director + departments). Default off — Agent will "
                   "not auto-invoke company; use Swarm / Eng. Firm modes or "
                   "turn this on when ready.");

        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Auto-route missions", &settings.autoMissionRouting))
            changed = true;
        helpMarker("When on, Agent mode may auto-launch a mission (intake → "
                   "breakdown → specialists) for build+analyze phrasing. "
                   "Default off — the main chat stays on one direct agent "
                   "turn with tools.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Full tool loadout (main chat)", &settings.directAgentFullTools))
            changed = true;
        helpMarker("When on, the main-chat agent gets the full MCP catalog "
                   "(still respecting per-tool Options and role family "
                   "assignments). When off, it starts lean and pulls packs "
                   "via enable_tools. Saved as agent.direct_agent_full_tools.");

        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Vision (send screenshots)", &settings.vision)) changed = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Attach viewport screenshots so the model can see the part.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Offline mode (local only)", &settings.offlineMode)) changed = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Refuses any cloud provider. Only a local loopback endpoint is allowed.");

        ImGui::TableNextColumn();
        ImGui::Checkbox("Show assistant thoughts", &ui::assistantThoughtsVisible());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Show the AI's reasoning steps inline in the conversation.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Project memory (per-folder)", &settings.memoryEnabled)) changed = true;
        helpMarker("Remembers decisions and notes under the PLM project "
                   "(.plm/memory.db). Auto-created; recall is token-budgeted.");

        ImGui::TableNextColumn();
        {
            bool redact = std::strcmp(settings.complianceMode, "redact") == 0;
            if (ImGui::Checkbox("Redact sensitive data (local, IP-safe)", &redact)) {
                std::memset(settings.complianceMode, 0, sizeof(settings.complianceMode));
                std::strncpy(settings.complianceMode, redact ? "redact" : "off",
                             sizeof(settings.complianceMode) - 1);
                saveSettings(st.settings);
            }
            helpMarker("Scrubs dimensions, materials, part/drawing numbers, and project "
                       "ids from outbound cloud traffic and rehydrates replies; images "
                       "are blocked. Uses your local redaction-role model (Settings -> "
                       "AI -> Roles -> redaction) for a residual pass. Compliance/IP "
                       "protection for cloud providers.");
        }
        ImGui::TableNextColumn();
        // Moved out of the retired Experimental tab: a modeling-behaviour preference, so it
        // belongs beside the other run preferences (and rides along into the AI workspace's
        // Options view, which shares this grid).
        if (ImGui::Checkbox("Editable feature sequence",
                            &settings.expCadSequenceMode))
            changed = true;
        helpMarker("Nudges the modeler to build the part as an ordered sequence of "
                   "individually editable features (sketch -> extrude/revolve/loft/sweep -> "
                   "fillet/chamfer/hole/pattern) that reconstructs a clean design history, "
                   "rather than one fused monolithic solid \xe2\x80\x94 the DeepCAD / CAD-Coder "
                   "command-sequence direction. Saved under config.json "
                   "experimental.cad_sequence_mode.");
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 6.0f * sc));

    // ---- Sliders / budgets in 2 columns ----
    if (ImGui::BeginTable("##harness_sliders", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Refine-pass cap");
        ImGui::SameLine();
        helpMarker("0 = automatic (stops once the render matches).");
        ImGui::SetNextItemWidth(-1.0f);
        if (ImGui::SliderInt("##h_miters", &settings.maxIters, 0, 64,
                             settings.maxIters == 0 ? "automatic" : "%d passes"))
            changed = true;

        ImGui::TableNextColumn();
        {
            static int s_maxDepth = -1;
            if (s_maxDepth < 0) s_maxDepth = ui::loadMaxSubagentDepth();
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Max subagent nesting");
            ImGui::SameLine();
            helpMarker("Nesting levels of subagent-spawning-subagent (not concurrency). "
                       "Root chat = depth 0. Saved to config.json agent.max_subagent_depth.");
            ImGui::SetNextItemWidth(-1.0f);
            int depth = s_maxDepth;
            if (ImGui::SliderInt("##h_depth", &depth, 0, 4, "%d")) {
                if (depth < 0) depth = 0;
                if (depth > 4) depth = 4;
                s_maxDepth = depth;
                ui::saveMaxSubagentDepth(depth);
            }
        }

        ImGui::TableNextColumn();
        if (settings.memoryEnabled) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory token budget");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::SliderInt("##h_membudget", &settings.memoryTokenBudget, 100, 2000,
                                 "%d tokens"))
                changed = true;
        } else {
            ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        }

        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Eng. Firm budget (USD)");
        ImGui::SameLine();
        helpMarker("Used when the composer is in Eng. Firm mode.");
        ImGui::SetNextItemWidth(-1.0f);
        double b = settings.companyBudgetUsd;
        if (ImGui::InputDouble("##h_engfirmbudget", &b, 5.0, 25.0, "$%.2f")) {
            if (b < 0.0) b = 0.0;
            settings.companyBudgetUsd = b;
            changed = true;
        }
        ImGui::EndTable();
    }
    return changed;
}

// Settings -> AI -> Roles: the command-model / vision-model split (Seek-CAD style). Moved here
// from the retired Experimental tab — it IS a model-role assignment, so it belongs beside the
// other per-role pickers rather than in a separate tab.
static void drawVisionRoutingSection(AppState& st, float sc) {
    Settings& settings = st.settings;

    // Command model = the active main provider/model (emits + parses the CAD ops). Chosen in
    // Settings -> Providers / the composer's model chip; shown read-only so both routes are
    // visible side by side. The cloud-compat slot hosts many services, so name the SERVICE
    // (Moonshot / DeepSeek / ...) instead of the generic "OpenRouter / Cloud API" slot label.
    ImGui::TextUnformatted("Command model (sends & parses CAD ops)");
    helpMarker("The model that drives the agent loop: understands the request, plans, and "
               "emits/parses the CAD operations. Pick it in Settings -> Providers or the "
               "composer's model chip. Shown here so you can see both routes side by side.");
    {
        const char* provLabel =
            ui::providerLabelForEndpoint(settings.provider, settings.baseUrl);
        char cm[600];
        std::snprintf(cm, sizeof(cm), "   %s  \xc2\xb7  %s", provLabel,
                      settings.model[0] ? settings.model : "(default)");
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                                                    ui::palette.accent[2], 1));
        ImGui::TextUnformatted(cm);
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 6 * sc));
    ImGui::Checkbox("Route vision & spatial reasoning through a separate model",
                    &settings.expVisionReview);
    helpMarker("When on, a vision model judges the rendered viewport during the review/fix "
               "stages (spatial awareness), while the command model above plans and emits ops "
               "— the Seek-CAD split. It can be local (ITAR-safe) or a cloud vision model. "
               "When off, the command model handles vision too (if it supports images). "
               "Refused under Offline mode if cloud.");
    dimText("Command model emits ops; this vision model sees the screenshot and drives fixes.");
    if (!settings.expVisionReview) return;

    ImGui::Dummy(ImVec2(0, 2 * sc));
    ImGui::TextUnformatted("Vision / spatial model");
    // Vision-capable presets: cloud (Qwen-VL / Kimi) + local.
    std::vector<const char*> vlabels;
    std::vector<const ui::LocalPreset*> vps;
    vlabels.push_back("(custom / type below)");
    vps.push_back(nullptr);
    for (const auto& p : ui::cloudPresets())
        if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
    for (const auto& p : ui::localPresets())
        if (p.vision) { vlabels.push_back(p.label.c_str()); vps.push_back(&p); }
    int vsel = 0;
    for (int i = 1; i < (int)vps.size(); ++i)
        if (vps[i] && vps[i]->id == settings.expVisionModel) vsel = i;
    ImGui::SetNextItemWidth(-1);
    if (ImGui::Combo("##expvispreset", &vsel, vlabels.data(), (int)vlabels.size()) && vsel > 0 &&
        vps[vsel]) {
        const ui::LocalPreset* p = vps[vsel];
        std::strncpy(settings.expVisionModel, p->id.c_str(), sizeof(settings.expVisionModel) - 1);
        std::strncpy(settings.expVisionBaseUrl, p->baseUrl.c_str(),
                     sizeof(settings.expVisionBaseUrl) - 1);
        // Prefill the credential from the preset's OWN service slot. Blanket-copying cloudKey
        // here shipped an EMPTY key for every Moonshot pick (the Kimi credential lives in
        // keys.moonshot), and the core then fell back to the "lm-studio" placeholder — a
        // guaranteed HTTP 401 that surfaced as stop_reason=provider_auth.
        const char* svc = ui::cloudServiceKey(p->baseUrl);
        const char* src = "";
        if (!ui::baseUrlIsLoopback(p->baseUrl)) {
            if (std::strcmp(svc, "moonshot") == 0) src = settings.moonshotKey;
            else src = settings.cloudKey;
        }
        std::memset(settings.expVisionKey, 0, sizeof(settings.expVisionKey));
        std::strncpy(settings.expVisionKey, src, sizeof(settings.expVisionKey) - 1);
    }
    ImGui::TextUnformatted("Model id");
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText("##expvismodel", settings.expVisionModel, sizeof(settings.expVisionModel));
    ImGui::TextUnformatted("Endpoint (base URL)");
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText("##expvisbase", settings.expVisionBaseUrl, sizeof(settings.expVisionBaseUrl));
    ImGui::TextUnformatted("API key (blank for a local endpoint)");
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText("##expviskey", settings.expVisionKey, sizeof(settings.expVisionKey),
                     ImGuiInputTextFlags_Password);
    helpMarker("Local: LM Studio / Ollama / vLLM (loopback, no key needed). Cloud: a vision "
               "model endpoint + its API key (refused under Offline mode).");
    // A remote endpoint with no key can only 401, and the core then SKIPS the split silently
    // (review falls back to the command model). Say so here instead of letting it look enabled.
    if (!settings.expVisionKey[0] && !ui::baseUrlIsLoopback(settings.expVisionBaseUrl)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("No API key for this cloud vision endpoint — the split is skipped "
                           "and the command model handles vision.");
        ImGui::PopStyleColor();
    }
}

// ---- agent.* memory/learning knobs (config.json) -----------------------------
// Same load-modify-write recipe as ui::loadMaxSubagentDepth/saveMaxSubagentDepth
// (copilot/settings.cpp): read with a default, save through updateConfigJson().
static std::string loadAgentConfigString(const char* key, const char* fallback) {
    std::string v = fallback;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(key) && agent[key].is_string())
                    v = agent[key].get<std::string>();
            }
        }
    } catch (...) {}
    return v;
}
static bool loadAgentConfigBool(const char* key, bool fallback) {
    bool v = fallback;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(key) && agent[key].is_boolean())
                    v = agent[key].get<bool>();
            }
        }
    } catch (...) {}
    return v;
}
static void saveAgentConfigValue(const char* key, const nlohmann::json& value) {
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"][key] = value;
    });
}

// Nested agent.<obj>.<key> variants (e.g. agent.compaction.supersede_reads): same
// load-modify-write, ensuring the intermediate object exists on write.
static bool loadAgentConfigNestedBool(const char* obj, const char* key, bool fallback) {
    bool v = fallback;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains(obj) && agent[obj].is_object() &&
                    agent[obj].contains(key) && agent[obj][key].is_boolean())
                    v = agent[obj][key].get<bool>();
            }
        }
    } catch (...) {}
    return v;
}
static void saveAgentConfigNestedBool(const char* obj, const char* key, bool value) {
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        if (!j["agent"].contains(obj) || !j["agent"][obj].is_object())
            j["agent"][obj] = nlohmann::json::object();
        j["agent"][obj][key] = value;
    });
}

// Memory & Learning + Workspace visibility knobs (Settings > AI > Harness).
// 2-col grid like the session knobs above. Backend/learning/cache knobs persist to
// config.json agent.* via the recipe above; visibility knobs persist via ui/ai/ai_prefs.
static void drawMemoryLearningSection(AppState& st, float sc) {
    Settings& settings = st.settings;

    static bool s_loaded = false;
    static int s_memBackend = 0;      // 0 sqlite | 1 mnemopi  (agent.memory_backend)
    // Unified compaction control (was two combos: backend + strategy). Maps to
    // agent.compaction_strategy + agent.compaction_backend together.
    // 0 Off | 1 Snapcompact (builtin) | 2 Snapcompact (OMP) | 3 Shake | 4 Summary
    static int s_compMode = 0;
    static bool s_supersedeReads = true;  // agent.compaction.supersede_reads
    static bool s_dropUseless = true;     // agent.compaction.drop_useless
    static bool s_learn = true;       // agent.memory_learn
    static bool s_autolearn = false;  // agent.autolearn_enabled
    static bool s_autoContinue = false; // agent.autolearn_auto_continue
    static int s_memModel = 0;        // 0 online | 1 local  (agent.memory_model)
    static int s_verbosity = 2;       // ui::aiUiVerbosity (0 quiet | 1 normal | 2 full)
    static bool s_follow = true;      // ui::aiFollowActiveSession
    static bool s_previews = true;    // ui::aiActiveWorkPreviews
    static bool s_cacheWarm = false;  // agent.cache_warming
    if (!s_loaded) {
        s_loaded = true;
        s_memBackend = (loadAgentConfigString("memory_backend", "sqlite") == "mnemopi") ? 1 : 0;
        {
            const std::string cs = loadAgentConfigString("compaction_strategy", "off");
            const bool omp = (loadAgentConfigString("compaction_backend", "builtin") == "omp");
            if (cs == "off" || cs.empty())
                s_compMode = 0;
            else if (omp && (cs == "snapcompact" || cs == "omp"))
                s_compMode = 2;
            else if (cs == "snapcompact")
                s_compMode = 1;
            else if (cs == "shake")
                s_compMode = 3;
            else if (cs == "summary")
                s_compMode = 4;
            else
                s_compMode = omp ? 2 : 0;
        }
        s_supersedeReads = loadAgentConfigNestedBool("compaction", "supersede_reads", true);
        s_dropUseless = loadAgentConfigNestedBool("compaction", "drop_useless", true);
        s_learn = loadAgentConfigBool("memory_learn", true);
        s_autolearn = loadAgentConfigBool("autolearn_enabled", false);
        s_autoContinue = loadAgentConfigBool("autolearn_auto_continue", false);
        s_memModel = (loadAgentConfigString("memory_model", "online") == "local") ? 1 : 0;
        s_verbosity = ui::aiUiVerbosity();
        s_follow = ui::aiFollowActiveSession();
        s_previews = ui::aiActiveWorkPreviews();
        s_cacheWarm = loadAgentConfigBool("cache_warming", false);
    }

    static const char* kMemBackends[] = {"SQLite", "Mnemopi"};
    static const char* kCompModes[] = {
        "Off",
        "Snapcompact (builtin)",
        "Snapcompact (OMP)",
        "Shake",
        "Summary",
    };
    static const char* kMemModels[] = {"Online", "Local"};
    static const char* kVerbosity[] = {"Quiet", "Normal", "Full"};

    auto saveCompMode = [&](int mode) {
        const char* strategy = "off";
        const char* backend = "builtin";
        switch (mode) {
        case 1: strategy = "snapcompact"; backend = "builtin"; break;
        case 2: strategy = "snapcompact"; backend = "omp"; break;
        case 3: strategy = "shake"; backend = "builtin"; break;
        case 4: strategy = "summary"; backend = "builtin"; break;
        default: strategy = "off"; backend = "builtin"; break;
        }
        saveAgentConfigValue("compaction_strategy", strategy);
        saveAgentConfigValue("compaction_backend", backend);
    };

    if (ImGui::BeginTable("##memory_learning", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory backend");
            ImGui::SameLine();
            helpMarker("Where project memory (decisions, notes, recall) is stored. "
                       "SQLite = local .plm/memory.db (default). Mnemopi = OMP bridge "
                       "memory. Saved to config.json agent.memory_backend.");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##mem_backend", &s_memBackend, kMemBackends, 2))
                saveAgentConfigValue("memory_backend", s_memBackend == 1 ? "mnemopi" : "sqlite");
        }
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Compaction");
            ImGui::SameLine();
            helpMarker("How long transcripts are compacted. Off = truncate only. "
                       "Snapcompact = snapshot-style archive (builtin Python or OMP "
                       "bridge). Shake = drop low-value turns. Summary = model-written "
                       "recap. Writes agent.compaction_strategy + agent.compaction_backend.");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##comp_mode", &s_compMode, kCompModes, 5))
                saveCompMode(s_compMode);
        }

        ImGui::TableNextColumn();
        ImGui::Checkbox("Project memory (per-folder)", &settings.memoryEnabled);
        helpMarker("Remembers decisions and notes under the PLM project "
                   "(.plm/memory.db). Auto-created; recall is token-budgeted.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Remember decisions & notes", &s_learn))
            saveAgentConfigValue("memory_learn", s_learn);
        helpMarker("Learning switch: when on, the agent writes decisions and notes it "
                   "judges worth keeping into project memory (agent.memory_learn, "
                   "default on). Recall still works when off; nothing new is learned.");

        ImGui::TableNextColumn();
        if (settings.memoryEnabled) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory token budget");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::SliderInt("##mem_budget_ml", &settings.memoryTokenBudget, 100, 2000,
                             "%d tokens");
        } else {
            ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        }
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Supersede stale reads", &s_supersedeReads))
            saveAgentConfigNestedBool("compaction", "supersede_reads", s_supersedeReads);
        helpMarker("When a file or artifact is re-read after it changed, the stale "
                   "earlier read is superseded in the compacted transcript "
                   "(agent.compaction.supersede_reads, default on).");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Drop useless turns", &s_dropUseless))
            saveAgentConfigNestedBool("compaction", "drop_useless", s_dropUseless);
        helpMarker("Compaction drops turns that contributed nothing (failed no-op "
                   "tool calls, empty replies) instead of archiving them "
                   "(agent.compaction.drop_useless, default on).");
        ImGui::EndTable();
    }

    // ---- OMP Auto-Learn (experimental) — matches OMP Memory tab knobs ----
    settingsSectionHeader("Auto-Learn (experimental)");
    dimText("OMP sidecar: after substantive turns, capture lessons into project "
            "memory. Standing guidance is prompt-cache neutral; auto-run capture "
            "uses an extra private write when enabled.");
    if (ImGui::BeginTable("##autolearn", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Auto-Learn (experimental)", &s_autolearn))
            saveAgentConfigValue("autolearn_enabled", s_autolearn);
        helpMarker("After the agent stops, nudge it to capture lessons to memory "
                   "(agent.autolearn_enabled). Standing guidance is injected into "
                   "the system prompt when on. Default off.");
        ImGui::TableNextColumn();
        if (!s_autolearn) ImGui::BeginDisabled();
        if (ImGui::Checkbox("Auto-run capture at stop", &s_autoContinue))
            saveAgentConfigValue("autolearn_auto_continue", s_autoContinue);
        helpMarker("When on, auto-write one project-memory note after a turn with "
                   "enough tool calls (agent.autolearn_auto_continue). Uses a little "
                   "extra work. When off, only standing Auto-Learn guidance remains.");
        if (!s_autolearn) ImGui::EndDisabled();

        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Memory model");
            ImGui::SameLine();
            helpMarker("Mnemopi LLM path for fact extraction when the OMP bridge "
                       "enables it: Online (default) or Local on-device. Persisted as "
                       "agent.memory_model. Bridge currently runs keyword/FTS-degraded "
                       "when LLM/embeddings are off.");
            ImGui::SetNextItemWidth(-1.0f);
            if (s_memBackend != 1) ImGui::BeginDisabled();
            if (ImGui::Combo("##mem_model", &s_memModel, kMemModels, 2))
                saveAgentConfigValue("memory_model", s_memModel == 1 ? "local" : "online");
            if (s_memBackend != 1) ImGui::EndDisabled();
        }
        ImGui::TableNextColumn();
        ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        ImGui::EndTable();
    }

    // ---- Workspace visibility: how much the AI command-center views show ----
    settingsSectionHeader("Workspace visibility");
    if (ImGui::BeginTable("##workspace_vis", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Verbosity");
            ImGui::SameLine();
            helpMarker("How much the Agent Workflow / Active Work / Session Plan views "
                       "render. Quiet = titles + status. Normal = last op. "
                       "Full = op tickers, counts, token/latency detail (default).");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ws_verbosity", &s_verbosity, kVerbosity, 3))
                ui::setAiUiVerbosity(s_verbosity);
        }
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Autopopulate (follow active session)", &s_follow))
            ui::setAiFollowActiveSession(s_follow);
        helpMarker("Views follow the ACTIVE chat session / latest run automatically "
                   "instead of manual picking.");

        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Active-Work previews", &s_previews))
            ui::setAiActiveWorkPreviews(s_previews);
        helpMarker("Render iso preview thumbnails for agent-touched documents in the "
                   "Active Work view. Off = lightweight placeholder only.");
        ImGui::TableNextColumn();
        if (ImGui::Checkbox("Provider cache-warming", &s_cacheWarm))
            saveAgentConfigValue("cache_warming", s_cacheWarm);
        ImGui::EndTable();
    }
    dimText("Cache-warming keeps provider prompt caches warm between runs "
            "(agent.cache_warming, default off).");
}

}  // anonymous namespace

namespace {

// ---- Skills editor (Settings → Prompts → Skills; ported from dead overlay) ----
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

// ---- Context injection triggers (Settings → Prompts → Context) ----
struct OvTriggerRow {
    std::string id;
    std::string purpose;
    std::string source;   // builtin | override | user
    std::string pack;
    std::string exemplar;
    bool enabled = true;
    int budgetTokens = 250;
    int cooldownTurns = 8;
    std::vector<std::string> keywords;
    std::vector<std::string> tools;
    std::vector<std::string> events;
};
struct OvTriggersUi {
    std::vector<OvTriggerRow> rows;
    bool loaded = false;
    bool rpcMissing = false;
    bool listInFlight = false;
    bool bodyInFlight = false;
    bool saveInFlight = false;
    double lastList = -1e9;
    std::string lastError;
    std::string selected;
    // Editor buffers for the selected trigger.
    bool enabled = true;
    int budgetTokens = 250;
    int cooldownTurns = 8;
    char purpose[512] = {};
    char packName[96] = {};
    char keywords[2048] = {};   // one regex per line
    char tools[1024] = {};      // comma or newline separated
    char events[1024] = {};
    char exemplar[512] = {};
    std::vector<char> packBody{std::vector<char>(65536, 0)};
    bool bodyDirty = false;   // any editor field dirty
    bool packDirty = false;   // pack body specifically edited (avoid freezing shipped packs)
    nlohmann::json prerunCalcs = nlohmann::json::array();  // preserved from get/list
    bool creating = false;
    char newId[96] = {};
};
static OvTriggersUi s_ovTrig;

static bool ovTriggersAbsent(const core::RpcResult& r) {
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
static std::vector<std::string> ovSplitLinesOrCommas(const char* buf) {
    std::vector<std::string> out;
    if (!buf) return out;
    std::string cur;
    for (const char* p = buf; ; ++p) {
        const char c = *p;
        if (c == '\n' || c == '\r' || c == ',' || c == 0) {
            size_t a = 0, b = cur.size();
            while (a < b && std::isspace((unsigned char)cur[a])) ++a;
            while (b > a && std::isspace((unsigned char)cur[b - 1])) --b;
            if (b > a) out.emplace_back(cur.substr(a, b - a));
            cur.clear();
            if (c == 0) break;
            continue;
        }
        cur.push_back(c);
    }
    return out;
}
static void ovJoinLines(const std::vector<std::string>& xs, char* buf, size_t n) {
    if (!buf || n == 0) return;
    buf[0] = 0;
    std::string s;
    for (size_t i = 0; i < xs.size(); ++i) {
        if (i) s.push_back('\n');
        s += xs[i];
    }
    if (s.size() >= n) s.resize(n - 1);
    std::memcpy(buf, s.data(), s.size());
    buf[s.size()] = 0;
}
static void ovPollTriggers(AppState& st, bool force) {
    if (s_ovTrig.rpcMissing) return;
    const double now = ImGui::GetTime();
    if (!force && s_ovTrig.loaded && (now - s_ovTrig.lastList) < 4.0) return;
    if (s_ovTrig.listInFlight) return;
    s_ovTrig.lastList = now;
    s_ovTrig.listInFlight = true;
    st.rpc.call("triggers_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_ovTrig.listInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            s_ovTrig.loaded = true;
            s_ovTrig.rows.clear();
            return;
        }
        if (!r.ok || !r.result.is_object()) {
            s_ovTrig.lastError = r.err.empty() ? "triggers_list failed" : r.err;
            s_ovTrig.loaded = true;
            return;
        }
        s_ovTrig.lastError.clear();
        s_ovTrig.rows.clear();
        auto arr = r.result.value("triggers", nlohmann::json::array());
        if (arr.is_array()) {
            for (const auto& e : arr) {
                if (!e.is_object()) continue;
                OvTriggerRow row;
                row.id = e.value("id", std::string());
                row.purpose = e.value("purpose", std::string());
                row.source = e.value("source", std::string("builtin"));
                row.enabled = e.value("enabled", true);
                row.budgetTokens = e.value("budget_tokens", 250);
                row.cooldownTurns = e.value("cooldown_turns", 8);
                auto inject = e.value("inject", nlohmann::json::object());
                if (inject.is_object()) {
                    row.pack = inject.value("context_pack", std::string());
                    row.exemplar = inject.value("exemplar", std::string());
                }
                auto match = e.value("match", nlohmann::json::object());
                if (match.is_object()) {
                    auto kws = match.value("keywords", nlohmann::json::array());
                    if (kws.is_array())
                        for (const auto& k : kws)
                            if (k.is_string()) row.keywords.push_back(k.get<std::string>());
                    auto tools = match.value("tools", nlohmann::json::array());
                    if (tools.is_array())
                        for (const auto& t : tools)
                            if (t.is_string()) row.tools.push_back(t.get<std::string>());
                    auto evs = match.value("events", nlohmann::json::array());
                    if (evs.is_array())
                        for (const auto& ev : evs)
                            if (ev.is_string()) row.events.push_back(ev.get<std::string>());
                }
                if (!row.id.empty()) s_ovTrig.rows.push_back(std::move(row));
            }
        }
        std::sort(s_ovTrig.rows.begin(), s_ovTrig.rows.end(),
                  [](const OvTriggerRow& a, const OvTriggerRow& b) {
                      return a.id < b.id;
                  });
        s_ovTrig.loaded = true;
    });
}
static void ovLoadTriggerBody(AppState& st, const std::string& id) {
    if (id.empty() || s_ovTrig.bodyInFlight) return;
    s_ovTrig.bodyInFlight = true;
    s_ovTrig.bodyDirty = false;
    s_ovTrig.packDirty = false;
    st.rpc.call("triggers_get", nlohmann::json{{"id", id}},
                [id](const core::RpcResult& r) {
        s_ovTrig.bodyInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_get failed"))
                : r.err;
            return;
        }
        if (s_ovTrig.selected != id) return;
        auto trig = r.result.value("trigger", nlohmann::json::object());
        auto pack = r.result.value("pack", nlohmann::json::object());
        s_ovTrig.enabled = trig.value("enabled", true);
        s_ovTrig.budgetTokens = trig.value("budget_tokens", 250);
        s_ovTrig.cooldownTurns = trig.value("cooldown_turns", 8);
        std::string purpose = trig.value("purpose", std::string());
        std::memset(s_ovTrig.purpose, 0, sizeof(s_ovTrig.purpose));
        std::strncpy(s_ovTrig.purpose, purpose.c_str(), sizeof(s_ovTrig.purpose) - 1);
        auto inject = trig.value("inject", nlohmann::json::object());
        std::string packName = inject.is_object()
            ? inject.value("context_pack", std::string()) : std::string();
        std::string exemplar = inject.is_object()
            ? inject.value("exemplar", std::string()) : std::string();
        s_ovTrig.prerunCalcs = (inject.is_object() && inject.contains("prerun_calcs")
                                && inject["prerun_calcs"].is_array())
            ? inject["prerun_calcs"] : nlohmann::json::array();
        std::memset(s_ovTrig.packName, 0, sizeof(s_ovTrig.packName));
        std::strncpy(s_ovTrig.packName, packName.c_str(), sizeof(s_ovTrig.packName) - 1);
        std::memset(s_ovTrig.exemplar, 0, sizeof(s_ovTrig.exemplar));
        std::strncpy(s_ovTrig.exemplar, exemplar.c_str(), sizeof(s_ovTrig.exemplar) - 1);
        auto match = trig.value("match", nlohmann::json::object());
        std::vector<std::string> kws, tools, evs;
        if (match.is_object()) {
            auto a = match.value("keywords", nlohmann::json::array());
            if (a.is_array()) for (const auto& k : a)
                if (k.is_string()) kws.push_back(k.get<std::string>());
            a = match.value("tools", nlohmann::json::array());
            if (a.is_array()) for (const auto& t : a)
                if (t.is_string()) tools.push_back(t.get<std::string>());
            a = match.value("events", nlohmann::json::array());
            if (a.is_array()) for (const auto& e : a)
                if (e.is_string()) evs.push_back(e.get<std::string>());
        }
        ovJoinLines(kws, s_ovTrig.keywords, sizeof(s_ovTrig.keywords));
        ovJoinLines(tools, s_ovTrig.tools, sizeof(s_ovTrig.tools));
        ovJoinLines(evs, s_ovTrig.events, sizeof(s_ovTrig.events));
        std::string body = pack.is_object() ? pack.value("body", std::string()) : std::string();
        std::fill(s_ovTrig.packBody.begin(), s_ovTrig.packBody.end(), 0);
        if (body.size() >= s_ovTrig.packBody.size())
            body.resize(s_ovTrig.packBody.size() - 1);
        std::memcpy(s_ovTrig.packBody.data(), body.data(), body.size());
        s_ovTrig.bodyDirty = false;
        s_ovTrig.packDirty = false;
        s_ovTrig.lastError.clear();
    });
}
static void ovSaveTrigger(AppState& st) {
    if (s_ovTrig.selected.empty() || s_ovTrig.saveInFlight) return;
    s_ovTrig.saveInFlight = true;
    nlohmann::json p;
    p["id"] = s_ovTrig.selected;
    p["enabled"] = s_ovTrig.enabled;
    p["purpose"] = std::string(s_ovTrig.purpose);
    p["budget_tokens"] = s_ovTrig.budgetTokens;
    p["cooldown_turns"] = s_ovTrig.cooldownTurns;
    p["match"] = nlohmann::json{
        {"keywords", ovSplitLinesOrCommas(s_ovTrig.keywords)},
        {"tools", ovSplitLinesOrCommas(s_ovTrig.tools)},
        {"events", ovSplitLinesOrCommas(s_ovTrig.events)},
    };
    p["inject"] = nlohmann::json{
        {"context_pack", std::string(s_ovTrig.packName)},
        {"prerun_calcs", s_ovTrig.prerunCalcs.is_array()
            ? s_ovTrig.prerunCalcs : nlohmann::json::array()},
        {"exemplar", std::string(s_ovTrig.exemplar)},
    };
    for (const auto& row : s_ovTrig.rows) {
        if (row.id == s_ovTrig.selected) {
            if (s_ovTrig.packName[0] == 0 && !row.pack.empty())
                p["inject"]["context_pack"] = row.pack;
            break;
        }
    }
    // Only write pack body when the user actually edited it (or is creating).
    // Toggling Enabled alone must not freeze a shipped pack into the user dir.
    if (s_ovTrig.packDirty || s_ovTrig.creating) {
        p["pack_body"] = std::string(s_ovTrig.packBody.data());
        p["pack_name"] = std::string(s_ovTrig.packName);
    }
    st.rpc.call("triggers_save", p, [](const core::RpcResult& r) {
        s_ovTrig.saveInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_save failed"))
                : r.err;
            return;
        }
        s_ovTrig.bodyDirty = false;
        s_ovTrig.packDirty = false;
        s_ovTrig.lastError.clear();
        s_ovTrig.loaded = false;
        s_ovTrig.lastList = -1e9;
    });
}
static void ovDeleteTriggerOverride(AppState& st, const std::string& id) {
    if (id.empty()) return;
    st.rpc.call("triggers_delete", nlohmann::json{{"id", id}},
                [id](const core::RpcResult& r) {
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_delete failed"))
                : r.err;
            return;
        }
        s_ovTrig.lastError.clear();
        s_ovTrig.loaded = false;
        s_ovTrig.lastList = -1e9;
        if (s_ovTrig.selected == id)
            s_ovTrig.bodyDirty = false;
    });
}

static void drawSettingsSkillsTab(AppState& st, float S) {
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

    // One scrollbar only: size columns to the remaining viewport minus the group title
    // Interwebz paints above the child (title + underline). Parent ##tab_ai must not also scroll.
    const float titleBudget = ImGui::GetTextLineHeight() + 10.0f * S;
    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - titleBudget);
    ui::InterwebzTwoColumnBegin(12.0f);
    if (ui::InterwebzGroupBegin("##set_skill_list", "Catalog", ImVec2(0, colH))) {
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
    if (ui::InterwebzGroupBegin("##set_skill_ed", "Editor", ImVec2(0, colH))) {
        if (s_ovSkills.creating) {
            ImGui::TextUnformatted("Name");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##set_newskill", s_ovSkills.newName, sizeof(s_ovSkills.newName));
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
                if (ImGui::InputTextMultiline("##set_skillbody", s_ovSkills.body.data(),
                                              s_ovSkills.body.size(), ImVec2(-1, edH)))
                    s_ovSkills.bodyDirty = true;
                if (st.fonts.mono) ImGui::PopFont();
            }

            const bool busySave = s_ovSkills.saveInFlight;
            if (busySave) ImGui::BeginDisabled();
            if (ImGui::Button(s_ovSkills.creating ? "Create##set_sksave" : "Save##set_sksave",
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

static void drawSettingsContextTab(AppState& st, float S) {
    ovPollTriggers(st, /*force=*/false);

    if (s_ovTrig.rpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "triggers_* RPC not available \xe2\x80\x94 Context Injection editor hidden.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_ovTrig.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_ovTrig.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const float titleBudget = ImGui::GetTextLineHeight() + 10.0f * S;
    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - titleBudget);
    ui::InterwebzTwoColumnBegin(10.0f);
    if (ui::InterwebzGroupBegin("##set_trig_list", "Triggers", ImVec2(0, colH))) {
        if (ImGui::Button("New trigger", ImVec2(-1, 0))) {
            s_ovTrig.creating = true;
            s_ovTrig.selected.clear();
            s_ovTrig.enabled = true;
            s_ovTrig.budgetTokens = 250;
            s_ovTrig.cooldownTurns = 8;
            std::memset(s_ovTrig.purpose, 0, sizeof(s_ovTrig.purpose));
            std::strncpy(s_ovTrig.purpose, "What this pack teaches the model.",
                         sizeof(s_ovTrig.purpose) - 1);
            std::memset(s_ovTrig.packName, 0, sizeof(s_ovTrig.packName));
            std::strncpy(s_ovTrig.packName, "my_pack", sizeof(s_ovTrig.packName) - 1);
            std::memset(s_ovTrig.keywords, 0, sizeof(s_ovTrig.keywords));
            std::strncpy(s_ovTrig.keywords, "\\bmy_keyword\\b",
                         sizeof(s_ovTrig.keywords) - 1);
            std::memset(s_ovTrig.tools, 0, sizeof(s_ovTrig.tools));
            std::memset(s_ovTrig.events, 0, sizeof(s_ovTrig.events));
            std::memset(s_ovTrig.exemplar, 0, sizeof(s_ovTrig.exemplar));
            std::fill(s_ovTrig.packBody.begin(), s_ovTrig.packBody.end(), 0);
            std::strncpy(s_ovTrig.packBody.data(),
                         "# My context pack\n\nImportant considerations:\n- ...\n",
                         s_ovTrig.packBody.size() - 1);
            s_ovTrig.prerunCalcs = nlohmann::json::array();
            s_ovTrig.bodyDirty = true;
            s_ovTrig.packDirty = true;
            std::memset(s_ovTrig.newId, 0, sizeof(s_ovTrig.newId));
            std::strncpy(s_ovTrig.newId, "my_trigger", sizeof(s_ovTrig.newId) - 1);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Create a user-only trigger + pack (saved under context_injection/).");
        ImGui::Dummy(ImVec2(0, 4.0f * S));

        if (!s_ovTrig.loaded && s_ovTrig.listInFlight) {
            ImGui::TextUnformatted("Loading triggers\xe2\x80\xa6");
        } else if (s_ovTrig.rows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("No triggers discovered.");
            ImGui::PopStyleColor();
        } else {
            for (size_t i = 0; i < s_ovTrig.rows.size(); ++i) {
                const auto& tr = s_ovTrig.rows[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovTrig.creating && s_ovTrig.selected == tr.id);
                if (ImGui::Selectable("##trigrow", sel, 0, ImVec2(0, 0))) {
                    s_ovTrig.creating = false;
                    s_ovTrig.selected = tr.id;
                    ovLoadTriggerBody(st, tr.id);
                }
                ImGui::SameLine(0, 0);
                ImGui::BeginGroup();
                ompStatusDotText(tr.id.c_str(),
                                 tr.enabled ? ui::palette.statusOk : ui::palette.textDim,
                                 tr.enabled, ui::palette.text);
                ImGui::SameLine();
                {
                    const char* chip = tr.source.c_str();
                    ImU32 chipCol = ui::Palette::toU32(ui::palette.textDim);
                    if (tr.source == "user")
                        chipCol = ui::Palette::toU32(ui::palette.accent);
                    else if (tr.source == "override")
                        chipCol = ui::Palette::toU32(ui::palette.statusOk);
                    ImGui::PushStyleColor(ImGuiCol_Text, chipCol);
                    ImGui::Text("[%s]", chip);
                    ImGui::PopStyleColor();
                }
                if (!tr.purpose.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("%s", tr.purpose.c_str());
                    ImGui::PopStyleColor();
                } else if (!tr.pack.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                               ui::palette.textDim[2], 1));
                    ImGui::TextWrapped("pack: %s", tr.pack.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::EndGroup();
                if (ImGui::IsItemClicked()) {
                    s_ovTrig.creating = false;
                    s_ovTrig.selected = tr.id;
                    ovLoadTriggerBody(st, tr.id);
                }
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 2.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##set_trig_ed", "Editor", ImVec2(0, colH))) {
        if (s_ovTrig.creating) {
            ImGui::TextUnformatted("Id");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##set_newtrig", s_ovTrig.newId, sizeof(s_ovTrig.newId));
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped(
                "Letters, digits, _ and - only. Saved as a user trigger.");
            ImGui::PopStyleColor();
        } else if (s_ovTrig.selected.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("Select a trigger to edit, or New trigger.");
            ImGui::PopStyleColor();
        } else {
            ui::InterwebzKeyValueRow("Trigger", s_ovTrig.selected.c_str());
            const char* src = "builtin";
            for (const auto& tr : s_ovTrig.rows)
                if (tr.id == s_ovTrig.selected) { src = tr.source.c_str(); break; }
            ui::InterwebzKeyValueRow("Source", src);
        }

        if (s_ovTrig.creating || !s_ovTrig.selected.empty()) {
            if (s_ovTrig.bodyInFlight && !s_ovTrig.creating) {
                ImGui::TextUnformatted("Loading\xe2\x80\xa6");
            } else {
                if (ui::InterwebzCheckbox("Enabled", &s_ovTrig.enabled))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Off = never inject this pack (user override of shipped).");

                ImGui::TextUnformatted("Purpose");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_purpose", s_ovTrig.purpose,
                                     sizeof(s_ovTrig.purpose)))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Why this pack exists — what important considerations it "
                        "forces the model to take.");

                ImGui::TextUnformatted("Keywords (regex, one per line)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputTextMultiline("##set_kws", s_ovTrig.keywords,
                        sizeof(s_ovTrig.keywords),
                        ImVec2(-1, 56.0f * S)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Tools (names that fire this)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_tools", s_ovTrig.tools,
                                     sizeof(s_ovTrig.tools)))
                    s_ovTrig.bodyDirty = true;
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Comma or newline separated tool names, e.g. part_place, fit_calc.");

                ImGui::TextUnformatted("Events");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_events", s_ovTrig.events,
                                     sizeof(s_ovTrig.events)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Context pack name");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_packname", s_ovTrig.packName,
                                     sizeof(s_ovTrig.packName)))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Exemplar (short worked example)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_exemplar", s_ovTrig.exemplar,
                                     sizeof(s_ovTrig.exemplar)))
                    s_ovTrig.bodyDirty = true;

                ImGui::SetNextItemWidth(ImMax(100.0f * S,
                    ImGui::GetContentRegionAvail().x * 0.48f));
                if (ImGui::SliderInt("Budget tokens", &s_ovTrig.budgetTokens,
                                     50, 2000))
                    s_ovTrig.bodyDirty = true;
                ImGui::SameLine();
                ImGui::SetNextItemWidth(ImMax(80.0f * S,
                    ImGui::GetContentRegionAvail().x * 0.9f));
                if (ImGui::SliderInt("Cooldown", &s_ovTrig.cooldownTurns, 0, 32))
                    s_ovTrig.bodyDirty = true;

                ImGui::TextUnformatted("Pack body (markdown injected into context)");
                const float edH = ImMax(80.0f * S,
                    ImGui::GetContentRegionAvail().y - 72.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##set_packbody",
                        s_ovTrig.packBody.data(), s_ovTrig.packBody.size(),
                        ImVec2(-1, edH))) {
                    s_ovTrig.bodyDirty = true;
                    s_ovTrig.packDirty = true;
                }
                if (st.fonts.mono) ImGui::PopFont();
            }

            const bool busySave = s_ovTrig.saveInFlight;
            if (busySave) ImGui::BeginDisabled();
            if (ImGui::Button(s_ovTrig.creating ? "Create##set_trsave" : "Save##set_trsave",
                              ImVec2(-1, 0))) {
                if (s_ovTrig.creating) {
                    std::string name = s_ovTrig.newId;
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) {
                            return !(std::isalnum(c) || c == '_' || c == '-');
                        }), name.end());
                    if (name.empty()) name = "my_trigger";
                    s_ovTrig.selected = name;
                    s_ovTrig.creating = false;
                    ovSaveTrigger(st);
                } else {
                    ovSaveTrigger(st);
                }
            }
            if (busySave) ImGui::EndDisabled();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Writes a user override (shipped triggers.yaml is never rewritten).");

            if (!s_ovTrig.creating && !s_ovTrig.selected.empty()) {
                bool isOverrideOrUser = false;
                bool isUserOnly = false;
                for (const auto& tr : s_ovTrig.rows) {
                    if (tr.id == s_ovTrig.selected) {
                        isOverrideOrUser = (tr.source == "override" || tr.source == "user");
                        isUserOnly = (tr.source == "user");
                        break;
                    }
                }
                if (isOverrideOrUser) {
                    ImGui::PushStyleColor(ImGuiCol_Button,
                        ImVec4(ui::palette.statusErr[0], ui::palette.statusErr[1],
                               ui::palette.statusErr[2], 0.55f));
                    if (ImGui::Button(
                            isUserOnly ? "Delete user trigger"
                                       : "Reset to shipped",
                            ImVec2(-1, 0)))
                        ovDeleteTriggerOverride(st, s_ovTrig.selected);
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip(
                            isUserOnly
                                ? "Remove this user-only trigger."
                                : "Clear the user override and restore the shipped trigger.");
                }
            }
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

}  // namespace (skills/context editors)

// ---- kSettingsEntries: unified command search table (command_search.cpp) ----
// Tab indices: 0=Appearance 1=General 2=AI 3=Providers. Benchmarks / Prompts / Agent /
// Experimental were absorbed by AI, so every one of their rows targets tab 2.
// One entry per prominent field/section; labels mirror the modal's visible text.
const SettingsEntry kSettingsEntries[] = {
    // Appearance (tab 0)
    { "Theme",                    "Appearance",   0 },
    { "Light theme",              "Appearance",   0 },
    { "Dark theme",               "Appearance",   0 },
    { "Liquid glass",             "Appearance",   0 },
    { "Glass tint",               "Appearance",   0 },
    { "Tint strength",            "Appearance",   0 },
    { "Glass level",              "Appearance",   0 },
    { "Glass opacity",            "Appearance",   0 },
    { "Corner rounding",          "Appearance",   0 },
    { "Text contrast",            "Appearance",   0 },
    { "Dynamic color",            "Appearance",   0 },
    // General (tab 1)
    { "Units",                    "General",      1 },
    { "MMGS units",               "General",      1 },
    { "IPS units",                "General",      1 },
    { "Workspace folder",         "General",      1 },
    { "Configurations",           "General",      1 },
    { "Engine routing",           "General",      1 },
    { "C++ core",                 "General",      1 },
    { "Performance profiler",     "General",      1 },
    { "Profiler",                 "General",      1 },
    // AI (tab 2) — harness (session knobs), roles, tools+maintenance
    { "Session knobs",            "AI",           2 },
    { "Harness",                  "AI",           2 },
    { "Tools",                    "AI",           2 },
    { "Dev Preview",              "AI",           2 },
    { "Prompt assembly",          "AI",           2 },
    { "Auto-Learn",               "AI",           2 },
    { "Compaction",               "AI",           2 },
    { "Tool families",            "AI",           2 },
    { "Bug reports",              "AI",           2 },
    { "Send bug reports",         "AI",           2 },
    { "Sketch 2D",                "AI",           2 },
    { "Model 3D",                 "AI",           2 },
    { "Reasoning effort",         "AI",           2 },
    { "Model roles",              "AI",           2 },
    { "Hidden models",            "AI",           2 },
    { "Harness organization",     "AI",           2 },
    { "Copilot mode",             "AI",           2 },
    { "Mode defaults",            "AI",           2 },
    { "Eng. Firm",                "AI",           2 },
    { "Eng. Firm model assignments", "AI",        2 },
    { "Swarm",                    "AI",           2 },
    // Providers (tab 3) — provider/model, keys, Moonshot, accounts
    { "AI provider",              "Providers",    3 },
    { "Provider & Model",         "Providers",    3 },
    { "Model",                    "Providers",    3 },
    { "API key",                  "Providers",    3 },
    { "Gemini key",               "Providers",    3 },
    { "Anthropic key",            "Providers",    3 },
    { "OpenAI key",               "Providers",    3 },
    { "Cloud API key",            "Providers",    3 },
    { "Moonshot",                 "Providers",    3 },
    { "Kimi",                     "Providers",    3 },
    { "Moonshot key",             "Providers",    3 },
    { "Vision",                   "Providers",    3 },
    { "Local endpoint",           "Providers",    3 },
    { "Accounts",                 "Providers",    3 },
    { "Subscription accounts",    "Providers",    3 },
    { "Sign in",                  "Providers",    3 },
    { "Add API key",              "Providers",    3 },
    { "OAuth",                    "Providers",    3 },
    { "Disconnect",               "Providers",    3 },
    { "Usage & cost",             "Providers",    3 },
    { "Session cost",             "Providers",    3 },
    { "Model routing",            "Providers",    3 },
    // Benchmarks (tab 4)
    { "Benchmarks",               "AI",    2 },
    { "Model leaderboard",        "AI",    2 },
    { "Run benchmark",            "AI",    2 },
    { "Benchmark",                "AI",    2 },
    { "Benchmark categories",     "AI",    2 },
    { "Category run",             "AI",    2 },
    // Prompts (tab 5)
    { "Prompts",                  "AI",           2 },
    { "Role prompts",             "AI",           2 },
    { "Agent prompts",            "AI",       2 },
    { "Prompt editor",            "AI",       2 },
    { "System prompt",            "AI",       2 },
    { "System prompt override",   "AI",       2 },
    { "Skills",                   "AI",       2 },
    { "Skill editor",             "AI",       2 },
    { "Context injection",        "AI",       2 },
    { "Triggers",                 "AI",       2 },
    // Run behavior (lives on AI tab; was Agent tab 6)
    { "Plan preference",          "AI",           2 },
    { "Plan before building",     "AI",           2 },
    { "Plan mode",                "AI",           2 },
    { "Subagent preference",      "AI",           2 },
    { "Role adoption",            "AI",           2 },
    { "Advisor preference",       "AI",           2 },
    { "Proactive advisor",        "AI",           2 },
    { "Project memory",           "AI",           2 },
    { "Memory budget",            "AI",           2 },
    { "Refine-pass cap",          "AI",           2 },
    { "Max iters",                "AI",           2 },
    { "Iteration budget",         "AI",           2 },
    { "Eng. Firm budget",         "AI",           2 },
    { "Company budget",           "AI",           2 },
    { "Run behavior",             "AI",           2 },
    { "Maintenance",              "AI",           2 },
    { "Max subagent nesting",     "AI",           2 },
    // Vision routing + parametric sequence (absorbed from the retired Experimental tab)
    { "Vision review model",      "AI",           2 },
    { "Vision routing",           "AI",           2 },
    { "Vision & spatial routing", "AI",           2 },
    { "Spatial model",            "AI",           2 },
    { "Seek-CAD split",           "AI",           2 },
    { "Parametric sequence mode", "AI",           2 },
    { "CAD sequence mode",        "AI",           2 },
    { "Feature sequence",         "AI",           2 },
    { "Monolithic solid",         "AI",           2 },
    { "Experimental",             "AI",           2 },
};
const int kSettingsEntryCount =
    (int)(sizeof(kSettingsEntries) / sizeof(kSettingsEntries[0]));


namespace ui {

// settings_modal.h publishes drawHarnessSessionKnobs as ui::drawHarnessSessionKnobs (the AI
// workspace reuses the same Options-tab run-behavior grid), but the definition above lives in
// this file's anonymous namespace — which left the exported symbol undefined at link time.
// Forward to it. (:: is required: unqualified here would resolve to this wrapper.)
bool drawHarnessSessionKnobs(AppState& st, float sc) {
    return ::drawHarnessSessionKnobs(st, sc);
}

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;

    // ---------- Settings window ----------
    static bool s_settingsOpen = false;
    // On-open snapshot for Cancel: the AI-tab picker and the Agent/Experimental checkboxes edit
    // st.settings live, so Cancel restores this copy (real revert semantics — audit P1). The
    // immediate-persist preferences (units / theme / glass) are re-kept on Cancel below.
    static Settings s_snapshot;
    bool opening = false;
    if (st.openSettings) {
        if (!s_settingsOpen) opening = true;
        s_settingsOpen = true;
        st.openSettings = false;
    }
    if (st.openSettingsTab >= 0 && !s_settingsOpen) { opening = true; s_settingsOpen = true; }
    // Workspace-folder edit buffer (General tab), reseeded from Settings on every open.
    static std::vector<char> s_wsBuf(512, 0);
    if (opening) {
        s_snapshot = settings;
        std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
        std::strncpy(s_wsBuf.data(), settings.workspaceDir.c_str(), s_wsBuf.size() - 1);
        // Make sure the glass engine reflects the persisted config before the Appearance
        // preview draws (idempotent — app startup wiring may already have pushed it).
        ui::setGlassConfig(settings.glassCfg);
        // Re-read eng_firm.models so the assignments grid matches disk on every open.
        s_efModels.loaded = false;
    }
    float sc = ui::uiScale;
    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float margin = 28.0f * sc;
    const float maxW = std::max(360.0f * sc, vp->WorkSize.x - margin * 2.0f);
    const float maxH = std::max(420.0f * sc, vp->WorkSize.y - margin * 2.0f);
    const float winW = std::min(920.0f * sc, maxW);
    const float winH = std::min(820.0f * sc, maxH);
    if (s_settingsOpen) {
        ImGui::SetNextWindowSize(ImVec2(winW, winH), ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
        // Opaque title strip — the global applyTheme sets TitleBg* to rgba(0,0,0,0) so floating
        // glass windows render transparently by default.  Override here so the Settings dialog
        // reads as a crisp native chrome dialog in both light and dark themes.
        ImGui::PushStyleColor(ImGuiCol_TitleBg,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        ImGui::PushStyleColor(ImGuiCol_TitleBgActive,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        ImGui::PushStyleColor(ImGuiCol_TitleBgCollapsed,
            ImVec4(ui::palette.headerBg[0], ui::palette.headerBg[1],
                   ui::palette.headerBg[2], 1.0f));
        bool settingsBegun = ImGui::Begin("Settings", &s_settingsOpen,
                         ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoDocking);
        ImGui::PopStyleColor(3);  // TitleBg / TitleBgActive / TitleBgCollapsed
        if (settingsBegun) {
        // C22b: if a pending prompt role was requested, load prompts and jump to the Prompts tab.
        bool jumpToPrompts = false;
        if (!st.pendingPromptRole.empty() && st.workerStarted) {
            if (!s_promptsLoaded) loadPrompts(st);
            // Select the matching role in the list
            for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                if (s_promptRecs[i].role == st.pendingPromptRole) {
                    s_selectedRole = i;
                    break;
                }
            }
            st.pendingPromptRole.clear();
            jumpToPrompts = true;
        }
        // Which AI sub-tab a jump should land on (-1 = leave the last one selected). Declared
        // out here because the legacy-index remap below runs BEFORE the tab bar is built.
        static int s_aiSubJump = -1;
        // Which AI sub-section the left nav rail has selected (persists across opens).
        static int s_aiSubSel = 0;
        // openSettingsTab: jump to a specific tab requested by the command search widget.
        int jumpToTab = -1;
        if (st.openSettingsTab >= 0) {
            jumpToTab          = st.openSettingsTab;
            st.openSettingsTab = -1;
        }
        // Tabs are 0=Appearance 1=General 2=AI 3=Providers. Every retired tab index folds onto
        // AI and selects the sub-tab that absorbed it: Benchmarks(4), Prompts(5), Agent(6),
        // Experimental(7 — its two knobs now live on Roles and Harness). This MUST run before
        // the tab bar: when it sat after, a legacy index selected nothing on the frame it
        // arrived and the jump was silently dropped.
        if (jumpToTab >= 4) {
            if (jumpToTab == 4) s_aiSubJump = 5;       // Benchmarks
            else if (jumpToTab == 5) s_aiSubJump = 2;  // Prompts
            else if (jumpToTab == 7) s_aiSubJump = 1;  // Experimental -> Roles
            jumpToTab = 2;
        }
        if (jumpToPrompts) {
            s_aiSubJump = 2;
            jumpToTab = 2;
        }
        // A resolved jump drives the rail selection directly, so it lands the same frame.
        if (s_aiSubJump >= 0) s_aiSubSel = s_aiSubJump;
        // wantSettings (screenshot/automation shot) opens the AI tab.
        static bool s_wantSettingsTabDone = false;
        if (st.wantSettings && !s_wantSettingsTabDone) {
            s_wantSettingsTabDone = true;
            if (jumpToTab < 0) jumpToTab = 2;
        }

        // Sticky tab bar: fixed header region (tabs) + scrolling body per tab.
        // Outer child is non-scrolling so the tab row never leaves the top; each
        // tab wraps its content in settingsTabBodyBegin/End so only the body scrolls.
        // Save/Cancel stay pinned under the whole tabs area.
        const float footerH = ImGui::GetFrameHeightWithSpacing() + 20.0f * sc;
        pushGlassChildStyle(10.0f);
        ImGui::BeginChild("##settabsarea",
                          ImVec2(0, std::max(120.0f * sc,
                                             ImGui::GetContentRegionAvail().y - footerH)),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        if (ImGui::BeginTabBar("##settabs")) {
            // ---- Appearance tab (FIRST): theme + colored liquid glass (v2.1) ----
            // The canonical theme control lives here (moved from General; the View-menu item
            // stays a synced shortcut). The glass knobs edit Settings::glassCfg per group and
            // live-apply via ui::setGlassConfig + persist via saveSettings on every change —
            // so the persisted "appearance.glass" block is no longer a dead round-trip.
            if (ImGui::BeginTabItem("Appearance", nullptr,
                                    (jumpToTab == 0) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_appearance");
                settingsSectionHeader("Theme", /*first=*/true);
                bool isLight = (ui::activeTheme == ui::Theme::Light);
                if (ImGui::Checkbox("Light theme (SolidWorks-style)", &isLight))
                    applyThemeNow(st, isLight);
                dimText("Switches the whole UI between the dark and light palettes. "
                        "Saved immediately.");

                settingsSectionHeader("Brand mark");
                dimText("The copilot logo is a live 3D projection of a rotating tesseract "
                        "(a 4-cube) — this picks how it turns. Saved immediately.");
                ImGui::Dummy(ImVec2(0, 4 * sc));
                {
                    // Each card is a hit box with the figure painted over it, so the preview is
                    // the SAME renderer the header uses — what you see here is what ships.
                    ImDrawList* bdl = ImGui::GetWindowDrawList();
                    const float capPad = 8.0f * sc;
                    const float lineH  = ImGui::GetTextLineHeight();
                    // Width comes from the WIDEST style name, not a round literal: at 96dp the
                    // captions ran past the card and had to be clipped mid-word.
                    float labelW = 0.0f;
                    for (int i = 0; i < (int)ui::TesseractAnim::Count; ++i)
                        labelW = std::max(labelW,
                            ImGui::CalcTextSize(ui::tesseractAnimName((ui::TesseractAnim)i)).x);
                    const float cardW = std::max(96.0f * sc, labelW + capPad * 2.0f);
                    const float cardH = 96.0f * sc;
                    const float capOff = cardH - lineH - 7.0f * sc;   // caption baseline, card-relative
                    const float figSz  = std::min(cardW, capOff) - 10.0f * sc;
                    const double now   = ImGui::GetTime();
                    for (int i = 0; i < (int)ui::TesseractAnim::Count; ++i) {
                        if (i) ImGui::SameLine(0.0f, 8.0f * sc);
                        char cid[24];
                        std::snprintf(cid, sizeof(cid), "##brandanim%d", i);
                        const ImVec2 mn = ImGui::GetCursorScreenPos();
                        ImGui::InvisibleButton(cid, ImVec2(cardW, cardH));
                        const bool sel = (settings.brandAnim == i);
                        if (ImGui::IsItemClicked()) {
                            settings.brandAnim = i;
                            saveSettings(settings);   // matches the theme/glass immediate-save path
                        }
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("%s",
                                ui::tesseractAnimBlurb((ui::TesseractAnim)i));
                        const ImVec2 mx(mn.x + cardW, mn.y + cardH);
                        const float* bg = sel ? ui::palette.accentSoft : ui::palette.button;
                        bdl->AddRectFilled(mn, mx, ui::Palette::toU32(bg),
                                           ui::metrics::rounding(8.0f));
                        if (sel)
                            bdl->AddRect(mn, mx, ui::Palette::toU32(ui::palette.accent),
                                         ui::metrics::rounding(8.0f), 0,
                                         ui::metrics::stroke(1.5f));
                        // Figure sits above the caption: centre it in the space left over the
                        // one text line so the mark never overlaps its own label.
                        const float capY = mn.y + capOff;
                        ui::drawTesseract(bdl,
                                          ImVec2((mn.x + mx.x) * 0.5f, (mn.y + capY) * 0.5f),
                                          figSz, now, (ui::TesseractAnim)i,
                                          ui::Palette::toU32(ui::palette.text));
                        const char* nm = ui::tesseractAnimName((ui::TesseractAnim)i);
                        const ImVec2 ts = ImGui::CalcTextSize(nm);
                        bdl->AddText(ImVec2((mn.x + mx.x) * 0.5f - ts.x * 0.5f, capY),
                                     ui::Palette::toU32(sel ? ui::palette.text
                                                            : ui::palette.textDim),
                                     nm);
                    }
                }
                ImGui::Dummy(ImVec2(0, 4 * sc));

                settingsSectionHeader("Liquid Glass");
                dimText("Per-surface styling for the glass panels floating over the viewport. "
                        "Dynamic color auto-tints each panel for max contrast against the live "
                        "model; pin a tint below to override it. Saved immediately.");
                ImGui::Dummy(ImVec2(0, 4 * sc));

                // Group selector — segmented pills. Global is the inheritance base: the other
                // groups fall back to it for any knob left at its default ("Reset" state).
                static int s_glassGroup = 0;
                static const char* kGroupLabel[(size_t)ui::GlassGroup::Count] = {
                    "Global", "Feature Manager", "Copilot", "Viewport HUD"};
                for (int i = 0; i < (int)ui::GlassGroup::Count; ++i) {
                    if (i) ImGui::SameLine(0.0f, 6.0f * sc);
                    bool selGrp = (s_glassGroup == i);
                    const float* bc = selGrp ? ui::palette.accentSoft : ui::palette.button;
                    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(bc[0], bc[1], bc[2], bc[3]));
                    if (ImGui::Button(kGroupLabel[i])) s_glassGroup = i;
                    ImGui::PopStyleColor();
                }

                const ui::GlassGroup grp = (ui::GlassGroup)s_glassGroup;
                ui::GlassStyle& g = settings.glassCfg.group[(size_t)grp];
                // Seed every widget from the group's EFFECTIVE (resolved) values so inherited
                // knobs display what actually renders; an edit pins a concrete value.
                const ui::GlassStyleResolved& res = ui::glassStyleResolved(grp);
                bool glassChanged = false;

                // Aligned label column (uiScale-correct).
                const float labelW = 150.0f * sc;
                const float fieldW = 230.0f * sc;
                auto fieldRow = [&](const char* label) {
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted(label);
                    ImGui::SameLine(labelW);
                    ImGui::SetNextItemWidth(fieldW);
                };

                ImGui::Dummy(ImVec2(0, 6 * sc));
                // Tint is hue+saturation only (brightness comes from the glass level/opacity),
                // so edit it through an HSV swatch with the value channel pinned at 1.
                fieldRow("Tint color");
                {
                    float rgb[3];
                    ImGui::ColorConvertHSVtoRGB(res.tintHue / 360.0f, res.tintSat, 1.0f,
                                                rgb[0], rgb[1], rgb[2]);
                    if (ImGui::ColorEdit3("##glasstint", rgb, ImGuiColorEditFlags_NoInputs)) {
                        float h, s2, v;
                        ImGui::ColorConvertRGBtoHSV(rgb[0], rgb[1], rgb[2], h, s2, v);
                        g.tintHue = h * 360.0f;
                        g.tintSat = s2;
                        glassChanged = true;
                    }
                    ImGui::SameLine();
                    dimText("(hue + saturation; strength below mixes it in)");
                }
                // Slider VALUES render beside the track (not centered inside it) — the teal grab
                // sat right on top of the in-track text and obscured it (visual-review fix).
                auto sliderValueText = [&](const char* txt) {
                    ImGui::SameLine(0.0f, 8.0f * sc);
                    ImGui::AlignTextToFramePadding();
                    ImGui::TextUnformatted(txt);
                };
                fieldRow("Tint strength");
                {
                    float v = res.tintStrength;
                    if (ImGui::SliderFloat("##glassstrength", &v, 0.0f, 1.0f, "")) {
                        g.tintStrength = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2f", v);
                    sliderValueText(v <= 0.0f ? "off" : vb);
                }
                fieldRow("Glass level");
                {
                    static const char* kLevels[] = {"Automatic", "Ultra thin", "Thin",
                                                    "Regular", "Thick"};
                    int lv = res.glassLevel + 1;  // -1 (auto) .. 3 -> 0 .. 4
                    if (lv < 0 || lv > 4) lv = 0;
                    if (ImGui::Combo("##glasslevel", &lv, kLevels, 5)) {
                        g.glassLevel = lv - 1;
                        glassChanged = true;
                    }
                }
                fieldRow("Opacity");
                {
                    float v = res.opacityMul;
                    if (ImGui::SliderFloat("##glassopacity", &v, 0.25f, 2.0f, "")) {
                        g.opacityMul = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2fx", v);
                    sliderValueText(vb);
                }
                fieldRow("Corner rounding");
                {
                    float v = res.rounding < 0.0f ? 12.0f : res.rounding;
                    if (ImGui::SliderFloat("##glassround", &v, 0.0f, 24.0f, "")) {
                        g.rounding = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.0f dp", v);
                    sliderValueText(res.rounding < 0.0f ? "automatic" : vb);
                }
                fieldRow("Text contrast boost");
                {
                    float v = res.textContrastBoost;
                    if (ImGui::SliderFloat("##glassboost", &v, 0.0f, 1.0f, "")) {
                        g.textContrastBoost = v;
                        glassChanged = true;
                    }
                    char vb[32];
                    std::snprintf(vb, sizeof(vb), "%.2f", v);
                    sliderValueText(v <= 0.0f ? "off" : vb);
                }
                ImGui::Dummy(ImVec2(0, 2 * sc));
                bool dyn = res.dynamicColor;
                if (ImGui::Checkbox("Dynamic color \xe2\x80\x94 auto max-contrast vs model",
                                    &dyn)) {
                    g.dynamicColor = dyn ? 1 : 0;
                    glassChanged = true;
                }
                dimText("On (default): each panel samples the live viewport and steers its tint "
                        "for legibility. Off: the pinned tint above is used as-is.");

                ImGui::Dummy(ImVec2(0, 4 * sc));
                if (ImGui::Button("Reset to defaults", ImVec2(150.0f * sc, 28.0f * sc))) {
                    g = ui::GlassStyle();  // all sentinels -> inherit Global / built-in defaults
                    glassChanged = true;
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Restore this group to inherited (Global / built-in) values");

                if (glassChanged) {
                    ui::setGlassConfig(settings.glassCfg);  // live-apply (idempotent)
                    saveSettings(settings);                 // glass knobs persist immediately
                }

                // Live preview through the ACTUAL glass pipeline (adaptivePanel + glassPanel):
                // over the viewport it frosts the real backdrop with this group's resolved style;
                // over chrome it falls back to the layered glass. One stable id per group (the
                // adaptive cache requires the same group every frame for a given id).
                settingsSectionHeader("Preview");
                {
                    static const char* kPreviewId[(size_t)ui::GlassGroup::Count] = {
                        "settings_glass_preview_global", "settings_glass_preview_fm",
                        "settings_glass_preview_copilot", "settings_glass_preview_hud"};
                    ImVec2 mn = ImGui::GetCursorScreenPos();
                    float pw = std::max(220.0f * sc, ImGui::GetContentRegionAvail().x);
                    float ph = 64.0f * sc;
                    ImRect pr(mn, ImVec2(mn.x + pw, mn.y + ph));
                    const ui::AdaptiveSurface& surf =
                        ui::adaptivePanel(ui::panelId(kPreviewId[s_glassGroup]), pr, grp);
                    ImDrawList* dl = ImGui::GetWindowDrawList();
                    ui::glassPanel(dl, pr, ui::GlassLevel::Regular, 12.0f, surf);
                    ImFont* pf = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();
                    float fs = ImGui::GetFontSize();
                    ui::glassText(dl, pf, fs, ImVec2(mn.x + 14.0f * sc, mn.y + 12.0f * sc),
                                  surf, kGroupLabel[s_glassGroup]);
                    dl->AddText(pf, fs * 0.88f,
                                ImVec2(mn.x + 14.0f * sc, mn.y + 12.0f * sc + fs * 1.35f),
                                ImGui::ColorConvertFloat4ToU32(surf.textSecondary),
                                "Adaptive text over this group's glass");
                    ImGui::Dummy(ImVec2(pw, ph));
                }
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // ---- General tab: Document (units) + System (engine readout, profiler) ----
            if (ImGui::BeginTabItem("General", nullptr,
                                    (jumpToTab == 1) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_general");
                settingsSectionHeader("Document", /*first=*/true);
                ImGui::TextUnformatted("Units");
                helpMarker("The unit system for dimension display and input. The model is always "
                           "stored in canonical millimeters; this only changes how lengths are "
                           "shown and how a bare typed number is read. Angles stay in degrees in "
                           "both systems. You can also click the MMGS/IPS badge in the status bar "
                           "to switch.");
                int unitSel = st.ips() ? 1 : 0;
                bool unitsChanged = false;
                unitsChanged |= ImGui::RadioButton("MMGS (millimeter, gram, second)", &unitSel, 0);
                unitsChanged |= ImGui::RadioButton("IPS (inch, pound, second)", &unitSel, 1);
                if (unitsChanged) {
                    settings.units = (unitSel == 1) ? Units::IPS : Units::MMGS;
                    // Saved immediately so the choice survives a Cancel (it is a display/document
                    // preference, like theme — and the status-bar toggle persists the same way).
                    saveSettings(settings);
                    st.addLog(std::string("[units] set to ") + (st.ips() ? "IPS" : "MMGS"));
                }
                dimText("Changes how lengths display/parse everywhere (saved immediately).");

                // Configurations moved here from the left-pane strip (Graph took that slot).
                settingsSectionHeader("Configurations");
                if (st.configPanel) {
                    st.configPanel->drawEmbedded(st);
                } else {
                    ui::drawConfigurationsBody(st);
                }

                // Workspace folder (audit P1): workspace_dir was persisted but had NO UI —
                // users had to hand-edit config.json to re-root the File Explorer.
                settingsSectionHeader("Workspace");
                ImGui::TextUnformatted("Workspace folder");
#ifdef __EMSCRIPTEN__
                // Web: default is the server workspace abs root (fsBackend), not the
                // literal "/workspace" stub from defaultWorkspaceDir().
                {
                    static std::string s_wsDefaultWeb;
                    if (s_wsDefaultWeb.empty()) {
                        s_wsDefaultWeb = chudware::fsBackend().defaultRoot();
                        if (s_wsDefaultWeb.empty() || s_wsDefaultWeb == "/workspace")
                            s_wsDefaultWeb = chudware::fsBackend().defaultRoot();
                    }
                    static const std::string s_wsHelpWeb =
                        "The File Explorer's root folder on the server workspace. "
                        "Leave empty to use the server default. Browse picks a "
                        "subfolder under the workspace.";
                    static const std::string s_wsHintWeb =
                        s_wsDefaultWeb.empty() ? std::string("(server default)")
                                               : ("(default: " + s_wsDefaultWeb + ")");
                    helpMarker(s_wsHelpWeb.c_str());
                    ImGui::SetNextItemWidth(-210.0f * sc);
                    ImGui::InputTextWithHint("##wsdir", s_wsHintWeb.c_str(),
                                             s_wsBuf.data(), s_wsBuf.size());
                    ImGui::SameLine();
                    if (ImGui::Button("Browse\xe2\x80\xa6", ImVec2(90.0f * sc, 0))) {
                        std::string chosen = openFolderDialog();
                        if (!chosen.empty() && chosen != "/workspace") {
                            std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
                            std::strncpy(s_wsBuf.data(), chosen.c_str(), s_wsBuf.size() - 1);
                        }
                    }
                    ImGui::SameLine();
                    if (ImGui::Button("Apply", ImVec2(100.0f * sc, 0))) {
                        std::string chosen = s_wsBuf.data();
                        // Never persist the literal '/workspace' stub — resolve real root.
                        if (chosen.empty() || chosen == "/workspace") {
                            chosen.clear();
                            settings.workspaceDir.clear();
                            st.workspaceDir.clear();
                            // ensureWorkspaceDir will re-resolve via fsBackend().defaultRoot().
                        } else {
                            settings.workspaceDir = chosen;
                            st.workspaceDir = chosen;
                        }
                        saveSettings(settings);
                        st.addLog("[ok] workspace folder set: " +
                                  (settings.workspaceDir.empty() ? std::string("(default)")
                                                                 : settings.workspaceDir));
                    }
                    dimText("Root of the File Explorer on the server workspace (saved on Apply).");
                }
#else
                // Truthful default label: defaultWorkspaceDir() applies the alias rule
                // (existing Documents/Chudware wins, else Documents/Chudware).
                static const std::string s_wsDefault = defaultWorkspaceDir();
                static const std::string s_wsHelp =
                    "The File Explorer's root folder and the default save location. "
                    "Leave empty to use " + s_wsDefault + ". Applied and saved with the "
                    "Apply button.";
                static const std::string s_wsHint = "(default: " + s_wsDefault + ")";
                helpMarker(s_wsHelp.c_str());
                ImGui::SetNextItemWidth(-110.0f * sc);
                ImGui::InputTextWithHint("##wsdir", s_wsHint.c_str(),
                                         s_wsBuf.data(), s_wsBuf.size());
                ImGui::SameLine();
                if (ImGui::Button("Apply", ImVec2(100.0f * sc, 0))) {
                    settings.workspaceDir = s_wsBuf.data();
                    saveSettings(settings);
                    // Re-resolve the live root: empty falls back to Documents/Chudware inside
                    // the Explorer's ensureWorkspaceDir.
                    st.workspaceDir = settings.workspaceDir;
                    st.addLog("[ok] workspace folder set: " +
                              (settings.workspaceDir.empty() ? std::string("(default)")
                                                             : settings.workspaceDir));
                }
                dimText("Root of the File Explorer and default save location (saved on Apply).");
#endif

                // Theme moved to the Appearance tab (the canonical control); engine routing +
                // profiler are status/launchers, folded under one System section (audit P2).
                settingsSectionHeader("System");
                // R5a (T-RF21): routing is unconditional — the C++ core IS the engine
                // (the CHUDWARE_CPP_* escape hatches were removed with the Python oracle).
                if (core::cpp_core().available()) {
                    ImGui::TextUnformatted("Active engine: C++ core (libchudware)");
                    helpMarker("The in-process C++ core is the only modeling engine — documents, "
                               "sketching, FEA/CFD, CAM, ECAD, firmware, sourcing and the AI agent "
                               "all run on it. Services-side Python handles orchestration surfaces "
                               "(parts catalog, reverse engineering, vendor sign-in, missions).");
                    dimText(("C++ core: libchudware loaded (ABI " +
                             core::cpp_core().abiVersion() + ")").c_str());
                    ImGui::BulletText("Modeling / documents / sketch: C++ core");
                    ImGui::BulletText("Simulation (FEA + CFD) / CAM / ECAD / firmware: C++ core");
                    ImGui::BulletText("AI agent (copilot runs): C++ core");
                } else {
                    ImGui::TextUnformatted("Active engine: UNAVAILABLE (libchudware not loaded)");
                    helpMarker("The C++ core (libchudware) is NOT loaded, and it is the only engine "
                               "(the Python oracle was removed). Set the CHUDWARE_LIBCHUDWARE "
                               "environment variable to the built library "
                               "(core/build-occt-linux/bin/libchudware.so) or stage it next to the "
                               "executable, then restart.");
                    dimText("C++ core: not loaded");
                }

                ImGui::Dummy(ImVec2(0, 8 * sc));
                dimText("Sampling profiler \xe2\x80\x94 see where modeling time goes (extrude / "
                        "fillet / boolean, into the kernel) as a live flame graph.");
                if (ImGui::Button("Open Profiler", ImVec2(160.0f * sc, 30.0f * sc)))
                    st.showProfilerWindow = true;
                ImGui::Dummy(ImVec2(0, 6 * sc));
                dimText(("All settings here are stored in " + configPath() + ".").c_str());
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }
            // ---- AI tab: unified hub (absorbed Benchmarks / Prompts / Agent / Experimental) ----
            // Tab indices: 0=Appearance 1=General 2=AI 3=Providers. s_aiSubJump is resolved
            // above, before the tab bar, so a jump lands on the right sub-tab the same frame.
            if (ImGui::BeginTabItem("AI", nullptr,
                                    (jumpToTab == 2) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_ai", /*scroll=*/false);

                // AI sub-navigation: left vertical rail + content pane, mirroring
                // drawOmpProviderManager's split. Under 640*sc the rail collapses to a combo
                // (design §3.1). Only the SELECTED section's body runs each frame —
                // Benchmarks' status polling relies on that.
                static const char* kAiSubLabels[8] = {
                    "Harness", "Roles", "Prompts", "Skills",
                    "Context", "Benchmarks", "Tools", "Dev Preview"};
                const bool aiNarrow = ImGui::GetContentRegionAvail().x < 640.0f * sc;
                if (aiNarrow) {
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::BeginCombo("##ai_sub_pick", kAiSubLabels[s_aiSubSel])) {
                        for (int i = 0; i < 8; ++i)
                            if (ImGui::Selectable(kAiSubLabels[i], i == s_aiSubSel))
                                s_aiSubSel = i;
                        ImGui::EndCombo();
                    }
                } else {
                    // Borderless nav rail: selection is a soft accent wash (not the design
                    // system's glow-border, which reads as a boxed outline at this size) —
                    // the Selectable's own Header colors are suppressed so only the wash and
                    // the label paint. A hairline divider separates rail from content.
                    const ImVec2 railMn = ImGui::GetCursorScreenPos();
                    const float railAvailH = ImGui::GetContentRegionAvail().y;
                    const float railW = 148.0f * sc;
                    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                        ImVec2(6.0f * sc, 6.0f * sc));
                    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                        ImVec2(6.0f * sc, 2.0f * sc));
                    ImGui::BeginChild("##ai_rail", ImVec2(railW, 0),
                                      ImGuiChildFlags_AlwaysUseWindowPadding,
                                      ImGuiWindowFlags_NoScrollbar |
                                          ImGuiWindowFlags_NoScrollWithMouse);
                    const ImVec4 kTransparent(0, 0, 0, 0);
                    ImGui::PushStyleColor(ImGuiCol_Header, kTransparent);
                    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, kTransparent);
                    ImGui::PushStyleColor(ImGuiCol_HeaderActive, kTransparent);
                    const float railRowH = ImGui::GetTextLineHeight() + 6.0f * sc;
                    for (int i = 0; i < 8; ++i) {
                        ImGui::PushID(i);
                        const bool selRow = (i == s_aiSubSel);
                        if (ImGui::Selectable("##ai_rail_row", selRow, 0, ImVec2(0, railRowH)))
                            s_aiSubSel = i;
                        const bool hovRow = ImGui::IsItemHovered();
                        ImDrawList* rdl = ImGui::GetWindowDrawList();
                        const ImVec2 rmn = ImGui::GetItemRectMin();
                        const ImVec2 rmx = ImGui::GetItemRectMax();
                        // Bare dp radius — coloredRowWash scales it internally.
                        if (selRow)
                            ui::coloredRowWash(rdl, rmn, rmx, 6.0f, ui::palette.accent,
                                               0.30f, /*selected=*/false);
                        else if (hovRow)
                            ui::coloredRowWash(rdl, rmn, rmx, 6.0f, ui::palette.accent,
                                               0.10f, /*selected=*/false);
                        rdl->AddText(
                            ImVec2(rmn.x + 8.0f * sc,
                                   rmn.y + (rmx.y - rmn.y - ImGui::GetFontSize()) * 0.5f),
                            ui::Palette::toU32(selRow ? ui::palette.accent : ui::palette.text),
                            kAiSubLabels[i]);
                        ImGui::PopID();
                    }
                    ImGui::PopStyleColor(3);
                    ImGui::EndChild();
                    ImGui::PopStyleVar(2);  // ItemSpacing, WindowPadding
                    {
                        float edge[4];
                        ui::Palette::withAlpha(ui::palette.glassBorder, edge, 0.6f);
                        const float dx = railMn.x + railW + 3.5f * sc;
                        ImGui::GetWindowDrawList()->AddLine(
                            ImVec2(dx, railMn.y), ImVec2(dx, railMn.y + railAvailH),
                            ui::Palette::toU32(edge));
                    }
                    ImGui::SameLine(0, 8.0f * sc);
                }
                ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                    ImVec2(4.0f * sc, 2.0f * sc));
                ImGui::BeginChild("##ai_sub_body", ImVec2(0, 0),
                                  ImGuiChildFlags_AlwaysUseWindowPadding,
                                  ImGuiWindowFlags_NoScrollbar);
                {
                    // Selection changed: start the new section at the top.
                    static int s_aiSubDrawnPrev = -1;
                    if (s_aiSubDrawnPrev != s_aiSubSel) {
                        ImGui::SetScrollY(0.0f);
                        s_aiSubDrawnPrev = s_aiSubSel;
                    }

                    // Harness is first: full quick-settings mirror + mode grid.
                    // (Behavior was absorbed here so knobs aren't duplicated.)
                    if (s_aiSubSel == 0) {
                        settingsGlassSectionBegin("##ai_harness_org", "Harness organization");
                        drawHarnessOrganizationSection(st, sc);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_harness_session", "Session knobs");
                        dimText("Same knobs as Copilot -> Quick settings, plus modal-only "
                                "options (role adoption, company auto-route, Eng. Firm budget).");
                        ImGui::Dummy(ImVec2(0, 4.0f * sc));
                        if (drawHarnessSessionKnobs(st, sc))
                            saveSettings(st.settings);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_memory", "Memory & Learning");
                        drawMemoryLearningSection(st, sc);
                        settingsGlassSectionEnd();
                    }



                    if (s_aiSubSel == 1) {
                        // Model pickers first (compact table).
                        settingsGlassSectionBegin("##ai_model_roles", "Model roles");
                        drawModelRolesSection(st, sc);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_vision_routing",
                                                  "Vision & spatial routing");
                        drawVisionRoutingSection(st, sc);
                        settingsGlassSectionEnd();
                        ImGui::Dummy(ImVec2(0, 8.0f * sc));
                        {
                            static bool s_efOpen = false;
                            ImGui::SetNextItemOpen(s_efOpen, ImGuiCond_Once);
                            if (ImGui::CollapsingHeader("Eng. Firm model assignments")) {
                                s_efOpen = true;
                                settingsGlassSectionBegin("##ai_eng_firm", "Eng. Firm");
                                drawEngFirmModelAssignments(st, sc);
                                settingsGlassSectionEnd();
                            } else {
                                s_efOpen = false;
                            }
                        }
                    }

                    if (s_aiSubSel == 2) {
                        // Full-height role prompt editor for power users / dev refinement.
                        bool canFetch = st.workerStarted && !busy;
                        if (!s_promptsLoaded && canFetch) loadPrompts(st);

                        ImGui::AlignTextToFramePadding();
                        ImGui::TextUnformatted("Role prompts");
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip(
                                "Edit per-role system prompts (pipeline + harness roles). "
                                "Overrides save next to the shipped YAML cards.");
                        ImGui::SameLine();
                        if (!canFetch) ImGui::BeginDisabled();
                        if (ImGui::SmallButton("Refresh##prompts_refresh")) {
                            s_promptsLoaded = false;
                            loadPrompts(st);
                        }
                        if (!canFetch) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                            ImGui::SetTooltip(canFetch
                                ? "Reload prompt catalog from the worker"
                                : "Start the worker to load role prompts");
                        if (!s_promptsError.empty()) {
                            ImGui::SameLine();
                            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
                            ImGui::TextUnformatted(s_promptsError.c_str());
                            ImGui::PopStyleColor();
                        } else if (!s_promptsLoaded) {
                            ImGui::SameLine();
                            dimText(st.workerStarted
                                ? "Loading\xe2\x80\xa6"
                                : "Worker not ready \xe2\x80\x94 start runtime to edit prompts.");
                        } else {
                            ImGui::SameLine();
                            dimText((std::to_string(s_promptRecs.size()) + " roles").c_str());
                        }

                        // Auto-select first role once the catalog lands.
                        if (s_promptsLoaded && !s_promptRecs.empty() &&
                            (s_selectedRole < 0 ||
                             (s_selectedRole > (int)s_promptRecs.size()))) {
                            s_selectedRole = 0;
                        }

                        const int sysIdx = (int)s_promptRecs.size();
                        const float listW = 168.0f * sc;
                        // Fill remaining viewport — single scrollbar inside list/editor only.
                        const float promptsH = ImMax(220.0f * sc,
                            ImGui::GetContentRegionAvail().y - 4.0f * sc);

                        ImGui::PushStyleColor(ImGuiCol_ChildBg,
                            ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                                   ui::palette.panelBg[2], 1));
                        // List and editor are one split view: same border, same inner padding.
                        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
                        ImGui::BeginChild("##rolelist", ImVec2(listW, promptsH),
                                          ImGuiChildFlags_Borders);
                        if (s_promptRecs.empty() && s_promptsLoaded) {
                            dimText("No role prompts returned.");
                        }
                        for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                            bool sel = (i == s_selectedRole);
                            auto& pr = s_promptRecs[i];
                            std::string lbl = pr.role;
                            if (pr.overridden) lbl += " *";
                            if (ImGui::Selectable(lbl.c_str(), sel))
                                s_selectedRole = i;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("%s%s", pr.role.c_str(),
                                                  pr.overridden ? " (overridden)" : "");
                        }
                        if (!s_promptRecs.empty() || true) ImGui::Separator();
                        {
                            std::string syslbl = "system (override)";
                            if (st.spBuf[0] != 0) syslbl += " *";
                            if (ImGui::Selectable(syslbl.c_str(), s_selectedRole >= sysIdx))
                                s_selectedRole = sysIdx;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Override the agent system prompt (advanced)");
                        }
                        ImGui::EndChild();
                        ImGui::PopStyleColor();  // ChildBg (list only — editor stays transparent)

                        ImGui::SameLine();

                        ImGui::BeginChild("##roleeditor", ImVec2(0, promptsH),
                                          ImGuiChildFlags_Borders);
                        if (s_selectedRole >= sysIdx) {
                            ImGui::PushFont(st.fonts.medium);
                            ImGui::TextUnformatted("System prompt override");
                            ImGui::PopFont();
                            dimText("Empty = built-in. Custom text fully replaces it \xe2\x80\x94 "
                                    "keep the result/parts contract. Applied by Save below.");
                            ImGui::PushFont(st.fonts.mono);
                            float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                            if (edH < 100.0f * sc) edH = 100.0f * sc;
                            ImGui::InputTextMultiline("##sysprompt", st.spBuf.data(), st.spBuf.size(),
                                                      ImVec2(-1, edH));
                            ImGui::PopFont();
                            if (!canFetch) ImGui::BeginDisabled();
                            if (ImGui::Button("Load built-in prompt")) {
                                nlohmann::json pr;
                                std::string perr;
                                if (st.worker.call("get_prompts", nlohmann::json::object(), pr, perr,
                                                   20000)) {
                                    std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                                    std::strncpy(st.spBuf.data(),
                                                 pr.value("system_prompt", "").c_str(),
                                                 st.spBuf.size() - 1);
                                } else {
                                    st.addLog("[err] get_prompts: " + perr);
                                }
                            }
                            if (!canFetch) ImGui::EndDisabled();
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Copy the built-in prompt into the editor");
                            ImGui::SameLine();
                            if (ImGui::Button("Clear (use built-in)"))
                                std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                        } else if (s_selectedRole >= 0 &&
                                   s_selectedRole < (int)s_promptRecs.size()) {
                            auto& pr = s_promptRecs[s_selectedRole];
                            if (pr.overridden) {
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(ui::palette.statusWarn[0], ui::palette.statusWarn[1],
                                           ui::palette.statusWarn[2], 1));
                                ImGui::TextUnformatted("[overridden]");
                                ImGui::PopStyleColor();
                                ImGui::SameLine();
                            }
                            ImGui::PushFont(st.fonts.medium);
                            ImGui::TextUnformatted(pr.role.c_str());
                            ImGui::PopFont();
                            int curLen = (int)std::strlen(pr.buf.data());
                            int delta  = curLen - pr.builtinLen;
                            if (delta != 0) {
                                ImGui::SameLine();
                                dimText(delta > 0 ? "(differs from built-in: longer)"
                                                  : "(differs from built-in: shorter)");
                            }
                            ImGui::PushFont(st.fonts.mono);
                            float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                            if (edH < 100.0f * sc) edH = 100.0f * sc;
                            ImGui::InputTextMultiline("##prompted", pr.buf.data(), pr.buf.size(),
                                                      ImVec2(-1, edH));
                            ImGui::PopFont();
                            if (!canFetch) ImGui::BeginDisabled();
                            if (primaryButton("Save", ImVec2(80.0f * sc, 28.0f * sc))) {
                                savePromptOverride(pr);
                                st.addLog("[ok] prompt saved: " + pr.role);
                            }
                            if (!canFetch) ImGui::EndDisabled();
                            ImGui::SameLine();
                            if (!canFetch) ImGui::BeginDisabled();
                            if (ImGui::Button("Reset to built-in", ImVec2(140.0f * sc, 28.0f * sc))) {
                                resetPromptOverride(pr, st);
                                st.addLog("[ok] prompt reset: " + pr.role);
                            }
                            if (!canFetch) ImGui::EndDisabled();
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Delete override and restore shipped prompt");
                        } else {
                            dimText("Select a role on the left to edit its prompt.");
                        }
                        ImGui::EndChild();
                        ImGui::PopStyleVar();  // WindowPadding shared by both panes
                    }


                    if (s_aiSubSel == 3) {
                        // Fill-height editors own their scrolling: zero the parent ONCE on
                        // entry, never every frame (that ate the wheel on this tab).
                        if (aiSubTabJustEntered(3)) ImGui::SetScrollY(0.0f);
                        drawSettingsSkillsTab(st, sc);
                    }

                    if (s_aiSubSel == 4) {
                        if (aiSubTabJustEntered(4)) ImGui::SetScrollY(0.0f);
                        drawSettingsContextTab(st, sc);
                    }

                    if (s_aiSubSel == 5) {
                // Shared leaderboard board (refreshed by runner / refresh button).
                static ui::leaderboard::Board s_lb;
                static bool s_lbLoaded = false;
                static double s_lbLast = -1e9;
                const double now = ImGui::GetTime();
                if (!s_lbLoaded || (now - s_lbLast) > 5.0) {
                    s_lb = ui::leaderboard::load();
                    s_lbLoaded = true;
                    s_lbLast = now;
                }

                settingsGlassSectionBegin("##bench_categories", "Categories");
                drawBenchCategories(st, sc);
                // Keep status polling alive while this tab is open.
                pollBenchStatus(st, s_bench.running, &s_lb);
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##bench_leaderboard", "Model Leaderboard");
                dimText("Deterministic gym pass rates from benchmarks/leaderboard/leaderboard.json. "
                        "MOCK badge when task_suite ends in -MOCK.");
                if (ImGui::SmallButton("Refresh##settings_lb")) {
                    s_lb = ui::leaderboard::load();
                    s_lbLast = ImGui::GetTime();
                    s_cats.lastPoll = -1e9;
                    s_cats.loaded = false;
                }
                ImGui::Dummy(ImVec2(0, 2 * sc));
                ui::leaderboard::drawCompactTable(s_lb, sc);
                drawBenchRunner(st, sc, s_lb, s_lbLast);
                settingsGlassSectionEnd();

                    }

                    if (s_aiSubSel == 6) {

                        settingsGlassSectionBegin("##ai_tools_global", "Global tool limits");
                        dimText("Defaults applied to every tool call. A per-tool Options override "
                                "(nonzero) always wins. Saved under config.json agent.*.");
                        static int s_globalTimeout = 0;
                        static int s_globalMaxCalls = 0;
                        static int s_globalSupervision = 4;
                        static bool s_loopGuardEnabled = true;
                        static int s_loopGuardThreshold = 3;
                        static bool s_globalLoaded = false;
                        static double s_globalLast = -1e9;
                        static bool s_globalDirty = false;
                        static std::string s_globalErr;
                        {
                            const double nowGl = ImGui::GetTime();
                            if (!s_globalLoaded || (nowGl - s_globalLast) > 2.0) {
                                s_globalLast = nowGl;
                                s_globalLoaded = true;
                                try {
                                    std::ifstream in(configPath());
                                    nlohmann::json j = nlohmann::json::object();
                                    if (in) { in >> j; }
                                    auto ag = j.value("agent", nlohmann::json::object());
                                    s_globalTimeout = ag.value("tool_timeout_s_default", 0);
                                    s_globalMaxCalls = ag.value("tool_max_calls_default", 0);
                                    s_globalSupervision = ag.value("supervision_interval_calls", 4);
                                    s_loopGuardEnabled = ag.value("tool_loop_guard_enabled", true);
                                    s_loopGuardThreshold = ag.value("tool_loop_guard_threshold", 3);
                                } catch (...) {
                                    s_globalTimeout = 0;
                                    s_globalMaxCalls = 0;
                                    s_globalSupervision = 4;
                                    s_loopGuardEnabled = true;
                                    s_loopGuardThreshold = 3;
                                }
                                if (s_globalTimeout < 0) s_globalTimeout = 0;
                                if (s_globalMaxCalls < 0) s_globalMaxCalls = 0;
                                if (s_globalSupervision < 0) s_globalSupervision = 4;
                                if (s_loopGuardThreshold < 1) s_loopGuardThreshold = 1;
                            }
                        }
                        const float gLabelW = 160.0f * sc;
                        const float gFieldW = 90.0f * sc;
                        auto globalFieldRow = [&](const char* label) {
                            ImGui::AlignTextToFramePadding();
                            ImGui::TextUnformatted(label);
                            ImGui::SameLine(gLabelW);
                            ImGui::SetNextItemWidth(gFieldW);
                        };
                        globalFieldRow("Tool timeout (s)");
                        if (ImGui::InputInt("##global_timeout_s", &s_globalTimeout)) {
                            if (s_globalTimeout < 0) s_globalTimeout = 0;
                            s_globalDirty = true;
                        }
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("0 = no timeout. Per-tool override wins.");
                        globalFieldRow("Max calls per turn");
                        if (ImGui::InputInt("##global_max_calls", &s_globalMaxCalls)) {
                            if (s_globalMaxCalls < 0) s_globalMaxCalls = 0;
                            s_globalDirty = true;
                        }
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("0 = unlimited. Per-tool override wins.");
                        globalFieldRow("Supervision interval");
                        if (ImGui::InputInt("##global_supervision", &s_globalSupervision)) {
                            if (s_globalSupervision < 0) s_globalSupervision = 0;
                            s_globalDirty = true;
                        }
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("Force an advisor verification pass every N tool "
                                              "calls. 0 = never.");
                        ImGui::AlignTextToFramePadding();
                        ImGui::TextUnformatted("Loop guard");
                        ImGui::SameLine(gLabelW);
                        if (ImGui::Checkbox("##loop_guard_enabled", &s_loopGuardEnabled))
                            s_globalDirty = true;
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("Refuse a tool call that repeats the exact same "
                                              "tool + arguments too many times in a row instead "
                                              "of dispatching it again (a real stuck loop, not "
                                              "just \"used this tool a lot\").");
                        if (s_loopGuardEnabled) {
                            ImGui::SameLine();
                            ImGui::SetNextItemWidth(gFieldW);
                            if (ImGui::InputInt("##loop_guard_threshold", &s_loopGuardThreshold)) {
                                if (s_loopGuardThreshold < 1) s_loopGuardThreshold = 1;
                                s_globalDirty = true;
                            }
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Consecutive identical calls before refusal "
                                                  "(default 3).");
                        }
                        if (s_globalDirty) {
                            ImGui::Dummy(ImVec2(0, 4.0f * sc));
                            if (!s_globalErr.empty())
                                ImGui::TextColored(ImVec4(1, 0.4f, 0.3f, 1), "%s",
                                                   s_globalErr.c_str());
                            if (ImGui::Button("Save global limits", ImVec2(180.0f * sc, 0))) {
                                if (updateConfigJson([&](nlohmann::json& j) {
                                        if (!j.contains("agent") || !j["agent"].is_object())
                                            j["agent"] = nlohmann::json::object();
                                        j["agent"]["tool_timeout_s_default"] =
                                            std::max(0, s_globalTimeout);
                                        j["agent"]["tool_max_calls_default"] =
                                            std::max(0, s_globalMaxCalls);
                                        j["agent"]["supervision_interval_calls"] =
                                            std::max(0, s_globalSupervision);
                                        j["agent"]["tool_loop_guard_enabled"] = s_loopGuardEnabled;
                                        j["agent"]["tool_loop_guard_threshold"] =
                                            std::max(1, s_loopGuardThreshold);
                                    })) {
                                    s_globalDirty = false;
                                    s_globalErr.clear();
                                    s_globalLast = -1e9;
                                } else {
                                    s_globalErr = "Failed to save global tool limits.";
                                }
                            }
                        }
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_tools_roles", "Default tools by role");
                        dimText("Pick a role, then assign which MCP tool families it gets by "
                                "default. Replaces global on/off family toggles. Saved under "
                                "config.json agent.role_tools. Per-tool Options below still apply.");

                        // ---- role_tools state (mirrors services/agent/config.py DEFAULT_ROLE_TOOLS)
                        static std::map<std::string, std::set<std::string>> s_roleTools;
                        static int s_roleToolsSel = 4;  // "default"
                        static bool s_roleToolsDirty = false;
                        static bool s_toolOptDirty = false;
                        static double s_roleToolsLast = -1e9;
                        static std::map<std::string, bool> s_toolEnabled;
                        static std::map<std::string, int> s_toolTimeout;
                        static std::map<std::string, int> s_toolMaxCalls;
                        static std::map<std::string, std::string> s_toolDefaults;
                        static std::map<std::string, std::string> s_toolNote;
                        // Included tool context (description_override) + per-tool schema_trim
                        // ( -1 inherit tier, 0 full paragraph, 1 first-sentence trim ).
                        static std::map<std::string, std::string> s_toolDescOverride;
                        static std::map<std::string, int> s_toolSchemaTrim;
                        static std::string s_toolOptErr;

                        static const char* kToolFamilies[] = {
                            "cad", "ecad", "cam", "em", "reveng", "plm", "parts",
                            "advisor", "ledger", "intake", "recon", "gsl",
                            "workspace", "research", "sim", "mission",
                            "office", "files", "shell"
                        };
                        static constexpr int kToolFamilyCount =
                            (int)(sizeof(kToolFamilies) / sizeof(kToolFamilies[0]));

                        // Sensible defaults — keep in sync with services/agent/config.py
                        // DEFAULT_ROLE_TOOLS.
                        auto seedDefaultRoleTools = [&]() {
                            s_roleTools.clear();
                            auto put = [&](const char* role, std::initializer_list<const char*> fams) {
                                std::set<std::string> s;
                                for (const char* f : fams) s.insert(f);
                                s_roleTools[role] = std::move(s);
                            };
                            put("advisor", {"advisor", "research", "ledger", "gsl"});
                            put("task", {"cad", "sim", "cam", "parts", "gsl", "files", "shell", "office"});
                            put("planning", {"mission", "advisor", "ledger", "research", "gsl", "workspace"});
                            put("vision", {"gsl", "cad"});
                            put("default", {"cad", "files", "shell", "office", "plm", "research",
                                            "parts", "gsl", "advisor", "workspace", "ledger",
                                            "sim", "cam", "ecad", "reveng", "mission"});
                            put("plm", {"plm", "files", "office", "ledger"});
                            put("redaction", {});
                            put("sketch2d", {"cad", "gsl", "ecad"});
                            put("model3d", {"cad", "sim", "parts", "gsl"});
                            put("memory", {"ledger", "plm", "files"});
                            put("cam", {"cam", "cad", "parts"});
                            put("sim", {"sim", "cad", "gsl"});
                            put("ecad", {"ecad", "parts", "research", "em"});
                            put("drawing", {"cad", "office", "gsl"});
                            put("reveng", {"reveng", "research", "cad", "parts"});
                        };

                        // Offline fallback catalog — generated tables
                        // (tools/gen_tool_panel_tables.py). Whenever the
                        // runtime is up, the LIVE catalog from the worker
                        // ai_tool_catalog RPC replaces them (below).
                        using FamTools = tool_panel_tables::FamTools;
                        static const auto& kFamToolMap = tool_panel_tables::kFamToolMap;
                        static constexpr int kFamToolMapN = tool_panel_tables::kFamToolMapN;
                        static const auto& kToolDescs = tool_panel_tables::kToolDescs;
                        static constexpr int kToolDescsN = tool_panel_tables::kToolDescsN;
                        static const auto& kToolGuidance = tool_panel_tables::kToolGuidance;
                        static constexpr int kToolGuidanceN = tool_panel_tables::kToolGuidanceN;

                        // Live tool catalog (worker ai_tool_catalog RPC) — the
                        // panel shows every registered tool with its real
                        // family + live description, and the curated
                        // tool_guidance defaults in the per-tool Options popup.
                        static bool s_liveCatTried = false;
                        static bool s_liveCatOk = false;
                        static double s_liveCatAt = -1e9;
                        static std::vector<std::string> s_liveFamilies;
                        static std::map<std::string, std::vector<std::string>> s_liveFamTools;
                        static std::map<std::string, std::string> s_liveDescs;
                        static nlohmann::json s_liveGuidance = nlohmann::json::object();


                        auto toolTooltip = [&](const char* tn) -> const char* {
                            if (!tn || !tn[0]) return "MCP tool.";
                            // Live catalog description (worker ai_tool_catalog) first.
                            if (s_liveCatOk) {
                                auto it = s_liveDescs.find(tn);
                                if (it != s_liveDescs.end() && !it->second.empty())
                                    return it->second.c_str();
                            }
                            // kToolDescs (generated fallback) is sorted by name — linear
                            // scan is fine at this size.
                            for (int i = 0; i < kToolDescsN; ++i) {
                                if (!std::strcmp(tn, kToolDescs[i].name))
                                    return kToolDescs[i].desc;
                            }
                            // Family-aware fallbacks — never the useless generic alone.
                            if (std::strncmp(tn, "reveng_", 7) == 0 ||
                                std::strncmp(tn, "get_reveng_", 11) == 0 ||
                                std::strncmp(tn, "list_reveng_", 12) == 0)
                                return "Reverse-engineering session / reconstruct tool.";
                            if (std::strncmp(tn, "plm_", 4) == 0)
                                return "Product lifecycle management tool.";
                            if (std::strncmp(tn, "office_", 7) == 0)
                                return "Office document read/edit tool.";
                            if (std::strncmp(tn, "cam_", 4) == 0)
                                return "CAM / machining tool.";
                            if (std::strncmp(tn, "ecad_", 5) == 0)
                                return "Electronics CAD tool.";
                            if (std::strncmp(tn, "fs_", 3) == 0)
                                return "Project filesystem tool.";
                            if (std::strncmp(tn, "research_", 9) == 0)
                                return "Research / lookup tool.";
                            if (std::strncmp(tn, "em_", 3) == 0)
                                return "Electromagnetics simulation tool.";
                            if (std::strncmp(tn, "dag_", 4) == 0 ||
                                std::strncmp(tn, "mission_", 8) == 0)
                                return "Mission / DAG orchestration tool.";
                            return "Agent tool — see Options for AI usage parameters.";
                        };

                        // Built-in curated guidance (generated kToolGuidance table)
                        // as a json object shaped like the live catalog's guidance
                        // entries — used when the worker ai_tool_catalog RPC has
                        // no entry for this tool. Linear scan, same as toolTooltip.
                        auto builtinGuidance = [&](const char* tn) -> nlohmann::json {
                            nlohmann::json g = nlohmann::json::object();
                            if (!tn || !tn[0]) return g;
                            for (int i = 0; i < kToolGuidanceN; ++i) {
                                if (!std::strcmp(tn, kToolGuidance[i].name)) {
                                    if (kToolGuidance[i].note && kToolGuidance[i].note[0])
                                        g["prompt_note"] = kToolGuidance[i].note;
                                    if (kToolGuidance[i].timeout_s > 0)
                                        g["timeout_s"] = kToolGuidance[i].timeout_s;
                                    if (kToolGuidance[i].max_calls > 0)
                                        g["max_calls_per_turn"] = kToolGuidance[i].max_calls;
                                    if (kToolGuidance[i].param_defaults_json &&
                                        kToolGuidance[i].param_defaults_json[0]) {
                                        nlohmann::json pd = nlohmann::json::parse(
                                            kToolGuidance[i].param_defaults_json,
                                            nullptr, false);
                                        if (pd.is_object() && !pd.empty())
                                            g["param_defaults"] = pd;
                                    }
                                    break;
                                }
                            }
                            return g;
                        };

                        auto famHelp = [](const char* fam) -> const char* {
                            if (!std::strcmp(fam, "cad")) return "Parametric modeling, drawings, measure, mates.";
                            if (!std::strcmp(fam, "sim")) return "FEA, CFD, and motion simulation.";
                            if (!std::strcmp(fam, "cam")) return "CAM jobs, stock, toolpaths, post.";
                            if (!std::strcmp(fam, "ecad")) return "Schematic + PCB tools.";
                            if (!std::strcmp(fam, "em")) return "Electromagnetics simulation.";
                            if (!std::strcmp(fam, "parts")) return "Catalog parts, BOM, quotes.";
                            if (!std::strcmp(fam, "reveng")) return "Reverse engineering from scans/photos.";
                            if (!std::strcmp(fam, "plm")) return "Project history, checkpoints, releases.";
                            if (!std::strcmp(fam, "research")) return "Web search, materials, eng calc.";
                            if (!std::strcmp(fam, "mission")) return "DAG / mission orchestration.";
                            if (!std::strcmp(fam, "gsl")) return "Geometry narrative describers + sketch health.";
                            if (!std::strcmp(fam, "advisor")) return "Steering advisor tools.";
                            if (!std::strcmp(fam, "office")) return "Read/edit Office + PDF (office_oxide extract).";
                            if (!std::strcmp(fam, "files")) return "Project filesystem read/write/glob/grep.";
                            if (!std::strcmp(fam, "shell")) return "Shell commands inside the project jail.";
                            if (!std::strcmp(fam, "ledger")) return "Mission ledger read/write.";
                            if (!std::strcmp(fam, "intake")) return "Requirements / intake confirmations.";
                            if (!std::strcmp(fam, "recon")) return "Mesh reconstruction helpers.";
                            if (!std::strcmp(fam, "workspace")) return "Workspace status, enable_tools, delegate.";
                            return "Tool family.";
                        };

                        // Pull the live tool catalog once the worker is up
                        // (retry every 15 s until it succeeds). One synchronous
                        // load on first open — same pattern as loadPrompts().
                        if (st.workerStarted &&
                            (!s_liveCatTried ||
                             (!s_liveCatOk && ImGui::GetTime() - s_liveCatAt > 15.0))) {
                            s_liveCatTried = true;
                            s_liveCatAt = ImGui::GetTime();
                            nlohmann::json p = nlohmann::json::object();
                            nlohmann::json r;
                            std::string err;
                            if (st.worker.call("ai_tool_catalog", p, r, err, 20000) &&
                                r.value("ok", false)) {
                                s_liveFamTools.clear();
                                s_liveDescs.clear();
                                s_liveGuidance = nlohmann::json::object();
                                for (const auto& row :
                                     r.value("tools", nlohmann::json::array())) {
                                    const std::string name = row.value("name", "");
                                    const std::string fam = row.value("family", "?");
                                    if (name.empty()) continue;
                                    s_liveFamTools[fam].push_back(name);
                                    s_liveDescs[name] = row.value("description", "");
                                    if (row.contains("guidance") &&
                                        row["guidance"].is_object() &&
                                        !row["guidance"].empty())
                                        s_liveGuidance[name] = row["guidance"];
                                }
                                // Canonical family order (kToolFamilies), then any
                                // families the config list does not know yet.
                                s_liveFamilies.clear();
                                for (int i = 0; i < kToolFamilyCount; ++i)
                                    if (s_liveFamTools.count(kToolFamilies[i]))
                                        s_liveFamilies.push_back(kToolFamilies[i]);
                                for (const auto& kv : s_liveFamTools)
                                    if (std::find(s_liveFamilies.begin(),
                                                  s_liveFamilies.end(),
                                                  kv.first) == s_liveFamilies.end())
                                        s_liveFamilies.push_back(kv.first);
                                s_liveCatOk = true;
                            }
                        }

                        const double nowTf = ImGui::GetTime();
                        // Never re-seed/reload while the user has unsaved edits: the 2 s
                        // poll used to wipe a freshly ticked family (e.g. intake for the
                        // default role) back to the on-disk state before Save was hit.
                        if (s_roleTools.empty() ||
                            (!s_roleToolsDirty && !s_toolOptDirty &&
                             (nowTf - s_roleToolsLast) > 2.0)) {
                            s_roleToolsLast = nowTf;
                            seedDefaultRoleTools();
                            try {
                                std::ifstream in(configPath());
                                nlohmann::json j = nlohmann::json::object();
                                if (in) { in >> j; }
                                auto ag = j.value("agent", nlohmann::json::object());
                                auto rt = ag.value("role_tools", nlohmann::json::object());
                                if (rt.is_object()) {
                                    for (auto it = rt.begin(); it != rt.end(); ++it) {
                                        if (!it.value().is_array()) continue;
                                        std::set<std::string> fams;
                                        for (const auto& v : it.value()) {
                                            if (v.is_string()) fams.insert(v.get<std::string>());
                                        }
                                        s_roleTools[it.key()] = std::move(fams);
                                    }
                                }
                                auto to = ag.value("tool_options", nlohmann::json::object());
                                if (to.is_object()) {
                                    for (auto it = to.begin(); it != to.end(); ++it) {
                                        const std::string tn = it.key();
                                        const auto& e = it.value();
                                        if (!e.is_object()) continue;
                                        if (e.contains("enabled") && e["enabled"].is_boolean())
                                            s_toolEnabled[tn] = e["enabled"].get<bool>();
                                        else
                                            s_toolEnabled[tn] = true;
                                        if (e.contains("timeout_s") && e["timeout_s"].is_number_integer())
                                            s_toolTimeout[tn] = e["timeout_s"].get<int>();
                                        if (e.contains("max_calls_per_turn") &&
                                            e["max_calls_per_turn"].is_number_integer())
                                            s_toolMaxCalls[tn] = e["max_calls_per_turn"].get<int>();
                                        if (e.contains("param_defaults"))
                                            s_toolDefaults[tn] = e["param_defaults"].dump(2);
                                        if (e.contains("prompt_note") && e["prompt_note"].is_string())
                                            s_toolNote[tn] = e["prompt_note"].get<std::string>();
                                        if (e.contains("description_override") &&
                                            e["description_override"].is_string())
                                            s_toolDescOverride[tn] =
                                                e["description_override"].get<std::string>();
                                        if (e.contains("schema_trim")) {
                                            if (e["schema_trim"].is_null())
                                                s_toolSchemaTrim[tn] = -1;
                                            else if (e["schema_trim"].is_boolean())
                                                s_toolSchemaTrim[tn] =
                                                    e["schema_trim"].get<bool>() ? 1 : 0;
                                        }
                                    }
                                }
                            } catch (...) {
                                seedDefaultRoleTools();
                            }
                        }

                        // Role badge picker row
                        if (s_roleToolsSel < 0 || s_roleToolsSel >= kRoleCount) s_roleToolsSel = 4;
                        ImGui::BeginGroup();
                        for (int i = 0; i < kRoleCount; ++i) {
                            ImGui::PushID(i + 9100);
                            const bool sel = (i == s_roleToolsSel);
                            if (sel) {
                                ImGui::PushStyleColor(ImGuiCol_Button,
                                    ImVec4(kRoleColors[i].x, kRoleColors[i].y,
                                           kRoleColors[i].z, 0.35f));
                                ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                                    ImVec4(kRoleColors[i].x, kRoleColors[i].y,
                                           kRoleColors[i].z, 0.50f));
                                ImGui::PushStyleColor(ImGuiCol_ButtonActive,
                                    ImVec4(kRoleColors[i].x, kRoleColors[i].y,
                                           kRoleColors[i].z, 0.65f));
                            }
                            drawRoleBadge(i, sc);
                            ImGui::SameLine(0.0f, 2.0f * sc);
                            char btn[64];
                            std::snprintf(btn, sizeof(btn), "%s##rolepick%d", kRoleLabels[i], i);
                            if (ImGui::SmallButton(btn)) s_roleToolsSel = i;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Edit default tool families for %s",
                                                  kRoleLabels[i]);
                            if (sel) ImGui::PopStyleColor(3);
                            if ((i + 1) % 5 != 0)
                                ImGui::SameLine(0.0f, 8.0f * sc);
                            ImGui::PopID();
                        }
                        ImGui::EndGroup();

                        ImGui::Dummy(ImVec2(0, 6.0f * sc));
                        {
                            const char* rk = kRoleKeys[s_roleToolsSel];
                            ImGui::AlignTextToFramePadding();
                            drawRoleBadge(s_roleToolsSel, sc);
                            ImGui::SameLine(0.0f, 6.0f * sc);
                            ImGui::Text("Families for %s", kRoleLabels[s_roleToolsSel]);
                            helpMarker("Checked families become this role's default tool "
                                       "surface (unioned onto the role card allowlist). "
                                       "Main-chat Default also respects "
                                       "\"Full tool loadout\" in Harness.");

                            std::set<std::string>& assigned = s_roleTools[rk];
                            if (ImGui::BeginTable("##role_fam_assign", 3,
                                                  ImGuiTableFlags_SizingStretchSame |
                                                  ImGuiTableFlags_NoPadOuterX)) {
                                for (int fi = 0; fi < kToolFamilyCount; ++fi) {
                                    const char* fam = kToolFamilies[fi];
                                    ImGui::TableNextColumn();
                                    bool on = assigned.count(fam) > 0;
                                    ImGui::PushID(fam);
                                    if (ImGui::Checkbox(fam, &on)) {
                                        if (on) assigned.insert(fam);
                                        else assigned.erase(fam);
                                        s_roleToolsDirty = true;
                                    }
                                    if (ImGui::IsItemHovered())
                                        ImGui::SetTooltip("%s", famHelp(fam));
                                    ImGui::PopID();
                                }
                                ImGui::EndTable();
                            }
                        }

                        ImGui::Dummy(ImVec2(0, 8.0f * sc));
                        dimText("Per-tool rows: checkbox enables a tool globally (dimmed = off); "
                                "Options overrides limits, included tool context, and AI usage "
                                "guidance. Hover a name for what it does.");
                        static char s_toolFilterBuf[64] = "";
                        ImGui::SetNextItemWidth(-1);
                        ImGui::InputTextWithHint("##tool_filter", "Filter tools...",
                                                 s_toolFilterBuf, sizeof(s_toolFilterBuf));
                        const std::string toolNeedle = toLowerAscii(s_toolFilterBuf);

                        // Only expand families assigned to the selected role (plus always
                        // show all when filtering so users can find disabled tools).
                        const std::set<std::string>& selFams =
                            s_roleTools[kRoleKeys[s_roleToolsSel]];
                        // Unified family→tools view: the LIVE catalog when the
                        // worker answered, else the generated fallback tables.
                        std::vector<std::pair<std::string, std::vector<std::string>>> famView;
                        if (s_liveCatOk) {
                            for (const auto& fam : s_liveFamilies)
                                famView.emplace_back(fam, s_liveFamTools[fam]);
                        } else {
                            for (int fi = 0; fi < kFamToolMapN; ++fi) {
                                const FamTools& ft = kFamToolMap[fi];
                                famView.emplace_back(
                                    ft.fam,
                                    std::vector<std::string>(ft.tools, ft.tools + ft.n));
                            }
                        }
                        for (const auto& famEntry : famView) {
                            const std::string& famName = famEntry.first;
                            const std::vector<std::string>& famToolList = famEntry.second;
                            const bool famAssigned = selFams.count(famName) > 0;
                            if (!toolNeedle.empty()) {
                                int famMatches = 0;
                                for (const auto& toolName : famToolList)
                                    if (toLowerAscii(toolName).find(toolNeedle)
                                        != std::string::npos)
                                        ++famMatches;
                                if (famMatches == 0) continue;
                                ImGui::SetNextItemOpen(true, ImGuiCond_Always);
                            } else if (!famAssigned) {
                                // Still list unassigned families collapsed so users can
                                // enable individual tools / browse Options.
                            }
                            char famLabel[96];
                            std::snprintf(famLabel, sizeof(famLabel), "%s%s",
                                          famName.c_str(), famAssigned ? "" : "  (not assigned)");
                            if (!ImGui::TreeNode(famLabel)) continue;
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("%s", famHelp(famName.c_str()));
                            for (const auto& toolName : famToolList) {
                                const char* tn = toolName.c_str();
                                if (!toolNeedle.empty() &&
                                    toLowerAscii(tn).find(toolNeedle) == std::string::npos)
                                    continue;
                                ImGui::PushID(tn);
                                bool en = s_toolEnabled.count(tn) ? s_toolEnabled[tn] : true;
                                if (ImGui::Checkbox("##en", &en)) {
                                    s_toolEnabled[tn] = en;
                                    s_toolOptDirty = true;
                                }
                                if (ImGui::IsItemHovered())
                                    ImGui::SetTooltip("Enable/disable this tool globally.");
                                ImGui::SameLine();
                                if (!en)
                                    ImGui::PushStyleColor(
                                        ImGuiCol_Text,
                                        ui::Palette::toU32(ui::palette.textDim));
                                ImGui::TextUnformatted(tn);
                                if (ImGui::IsItemHovered())
                                    ImGui::SetTooltip("%s", toolTooltip(tn));
                                if (!en) ImGui::PopStyleColor();
                                ImGui::SameLine();
                                if (ImGui::SmallButton("Options"))
                                    ImGui::OpenPopup("tool_opts");
                                if (ImGui::BeginPopup("tool_opts")) {
                                    ImGui::TextUnformatted(tn);
                                    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 360.0f * sc);
                                    ImGui::TextDisabled("%s", toolTooltip(tn));
                                    ImGui::PopTextWrapPos();
                                    ImGui::Separator();
                                    ImGui::TextDisabled("0 = use built-in / global default");
                                    const float labelW = 150.0f * sc;
                                    const float fieldW = 90.0f * sc;
                                    const float boxW = 360.0f * sc;
                                    auto popupFieldRow = [&](const char* label) {
                                        ImGui::AlignTextToFramePadding();
                                        ImGui::TextUnformatted(label);
                                        ImGui::SameLine(labelW);
                                        ImGui::SetNextItemWidth(fieldW);
                                    };
                                    int to = s_toolTimeout.count(tn) ? s_toolTimeout[tn] : 0;
                                    int mc = s_toolMaxCalls.count(tn) ? s_toolMaxCalls[tn] : 0;
                                    // Built-in curated defaults (tool_guidance,
                                    // live catalog) — dimmed placeholders; an
                                    // empty user field inherits them. Falls back
                                    // to the generated kToolGuidance table when
                                    // the live catalog has no entry for this
                                    // tool (worker down / RPC not answered yet).
                                    const nlohmann::json curated =
                                        (s_liveCatOk && s_liveGuidance.contains(tn))
                                            ? s_liveGuidance[tn]
                                            : builtinGuidance(tn);
                                    popupFieldRow("timeout_s");
                                    if (ImGui::InputInt("##timeout_s", &to)) {
                                        s_toolTimeout[tn] = std::max(0, to);
                                        s_toolOptDirty = true;
                                    }
                                    if (to == 0) {
                                        ImGui::SameLine();
                                        const int curT = curated.value("timeout_s", 0);
                                        if (curT > 0)
                                            ImGui::TextDisabled("(default: %ds)", curT);
                                        else if (s_globalTimeout > 0)
                                            ImGui::TextDisabled("(global: %ds)", s_globalTimeout);
                                        else
                                            ImGui::TextDisabled("(global: no timeout)");
                                    }
                                    popupFieldRow("max_calls_per_turn");
                                    if (ImGui::InputInt("##max_calls_per_turn", &mc)) {
                                        s_toolMaxCalls[tn] = std::max(0, mc);
                                        s_toolOptDirty = true;
                                    }
                                    if (mc == 0) {
                                        ImGui::SameLine();
                                        const int curM = curated.value("max_calls_per_turn", 0);
                                        if (curM > 0)
                                            ImGui::TextDisabled("(default: %d)", curM);
                                        else if (s_globalMaxCalls > 0)
                                            ImGui::TextDisabled("(global: %d)", s_globalMaxCalls);
                                        else
                                            ImGui::TextDisabled("(global: unlimited)");
                                    }

                                    // AI usage parameters / included tool context
                                    // (agent.tool_options.<name> — same shape as Python
                                    // get_tool_options).
                                    ImGui::Dummy(ImVec2(0, 4.0f * sc));
                                    ImGui::TextUnformatted("AI usage parameters");
                                    ImGui::Separator();

                                    int trimMode = s_toolSchemaTrim.count(tn)
                                                       ? s_toolSchemaTrim[tn] : -1;
                                    if (trimMode < -1 || trimMode > 1) trimMode = -1;
                                    ImGui::AlignTextToFramePadding();
                                    ImGui::TextUnformatted("schema_trim");
                                    if (ImGui::IsItemHovered())
                                        ImGui::SetTooltip(
                                            "How much of the tool description is included in "
                                            "the AI tools prompt. Inherit = tier default.");
                                    ImGui::SameLine(labelW);
                                    ImGui::SetNextItemWidth(boxW - labelW + fieldW);
                                    const char* trimLabels[] = {
                                        "Inherit tier", "Full paragraph", "First sentence"
                                    };
                                    // Map stored -1/0/1 -> combo 0/1/2
                                    int trimSel = (trimMode < 0) ? 0 : (trimMode == 0 ? 1 : 2);
                                    if (ImGui::Combo("##schema_trim", &trimSel, trimLabels, 3)) {
                                        s_toolSchemaTrim[tn] =
                                            (trimSel == 0) ? -1 : (trimSel == 1 ? 0 : 1);
                                        s_toolOptDirty = true;
                                    }

                                    static char descBuf[2048];
                                    static char defBuf[2048];
                                    static char noteBuf[1024];
                                    std::snprintf(descBuf, sizeof(descBuf), "%s",
                                        s_toolDescOverride.count(tn)
                                            ? s_toolDescOverride[tn].c_str() : "");
                                    std::snprintf(defBuf, sizeof(defBuf), "%s",
                                        s_toolDefaults.count(tn)
                                            ? s_toolDefaults[tn].c_str() : "{}");
                                    std::snprintf(noteBuf, sizeof(noteBuf), "%s",
                                        s_toolNote.count(tn) ? s_toolNote[tn].c_str() : "");

                                    // Live docstring (what the model gets when override empty).
                                    const char* liveDesc = toolTooltip(tn);
                                    ImGui::Dummy(ImVec2(0, 2.0f * sc));
                                    ImGui::TextUnformatted("Included tool context");
                                    if (ImGui::IsItemHovered())
                                        ImGui::SetTooltip(
                                            "description_override — replaces the MCP docstring "
                                            "in the AI tools prompt. Leave empty to use the "
                                            "live tool description shown below.");
                                    if (descBuf[0] == '\0' && liveDesc && liveDesc[0]) {
                                        ImGui::PushStyleColor(ImGuiCol_Text,
                                            ui::Palette::toU32(ui::palette.textDim));
                                        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + boxW);
                                        ImGui::TextWrapped(
                                            s_liveCatOk ? "Live (no override): %s"
                                                        : "Built-in (offline snapshot): %s",
                                            liveDesc);
                                        ImGui::PopTextWrapPos();
                                        ImGui::PopStyleColor();
                                    }
                                    if (ImGui::InputTextMultiline("##description_override",
                                            descBuf, sizeof(descBuf),
                                            ImVec2(boxW, 56 * sc))) {
                                        s_toolDescOverride[tn] = descBuf;
                                        s_toolOptDirty = true;
                                    }

                                    ImGui::TextUnformatted("AI usage guidance");
                                    if (ImGui::IsItemHovered())
                                        ImGui::SetTooltip(
                                            "prompt_note — appended as \"Note: ...\" under this "
                                            "tool in the AI tools prompt. Leave empty to "
                                            "inherit the built-in default (when one exists).");
                                    const std::string curNote = curated.value("prompt_note", "");
                                    if (noteBuf[0] == '\0') {
                                        ImGui::PushStyleColor(ImGuiCol_Text,
                                            ui::Palette::toU32(ui::palette.textDim));
                                        if (!curNote.empty()) {
                                            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + boxW);
                                            ImGui::TextWrapped("(default): %s", curNote.c_str());
                                            ImGui::PopTextWrapPos();
                                        } else {
                                            ImGui::TextUnformatted(
                                                "(empty — no Note: line in the tools prompt)");
                                        }
                                        ImGui::PopStyleColor();
                                    }
                                    if (ImGui::InputTextMultiline("##prompt_note",
                                            noteBuf, sizeof(noteBuf),
                                            ImVec2(boxW, 40 * sc))) {
                                        s_toolNote[tn] = noteBuf;
                                        s_toolOptDirty = true;
                                    }

                                    ImGui::TextUnformatted("Default arguments (JSON)");
                                    if (ImGui::IsItemHovered())
                                        ImGui::SetTooltip(
                                            "param_defaults — kwargs merged under the model's "
                                            "call (model-supplied values win).");
                                    if (defBuf[0] == '\0' || !std::strcmp(defBuf, "{}")) {
                                        if (curated.contains("param_defaults") &&
                                            curated["param_defaults"].is_object() &&
                                            !curated["param_defaults"].empty()) {
                                            ImGui::PushStyleColor(ImGuiCol_Text,
                                                ui::Palette::toU32(ui::palette.textDim));
                                            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + boxW);
                                            ImGui::TextWrapped("(default): %s",
                                                curated["param_defaults"].dump().c_str());
                                            ImGui::PopTextWrapPos();
                                            ImGui::PopStyleColor();
                                        } else {
                                            ImGui::PushStyleColor(ImGuiCol_Text,
                                                ui::Palette::toU32(ui::palette.textDim));
                                            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + boxW);
                                            ImGui::TextWrapped(
                                                "(none — the tool's own signature defaults "
                                                "apply; see \"Model receives\" below)");
                                            ImGui::PopTextWrapPos();
                                            ImGui::PopStyleColor();
                                        }
                                    }
                                    if (ImGui::InputTextMultiline("##param_defaults",
                                            defBuf, sizeof(defBuf),
                                            ImVec2(boxW, 60 * sc))) {
                                        s_toolDefaults[tn] = defBuf;
                                        s_toolOptDirty = true;
                                    }

                                    // ---- Model receives: real assembled tools-slot entry ----
                                    ImGui::Dummy(ImVec2(0, 6.0f * sc));
                                    ImGui::TextUnformatted("Model receives (preview)");
                                    ImGui::SameLine();
                                    helpMarker("Exact tools-slot block assembled the same way "
                                               "as tool_schemas.render_tools_block (signature + "
                                               "description + optional Note). Refresh pulls from "
                                               "the worker; offline falls back to the live "
                                               "docstring + your overrides.");
                                    static std::string s_previewTool;
                                    static std::string s_previewText;
                                    static std::string s_previewErr;
                                    static double s_previewAt = -1e9;
                                    const bool canPrev = st.workerStarted && !busy;
                                    auto buildLocalPreview = [&]() {
                                        std::string sig = std::string("- ") + tn + "(…)";
                                        std::string body;
                                        if (s_toolDescOverride.count(tn) &&
                                            !s_toolDescOverride[tn].empty())
                                            body = s_toolDescOverride[tn];
                                        else if (liveDesc)
                                            body = liveDesc;
                                        std::string entry = sig;
                                        if (!body.empty())
                                            entry += "\n    " + body;
                                        if (s_toolNote.count(tn) && !s_toolNote[tn].empty())
                                            entry += "\n    Note: " + s_toolNote[tn];
                                        else if (!curNote.empty())
                                            entry += "\n    Note: " + curNote;
                                        s_previewText =
                                            "[TOOLS] Call tools with EXACTLY these kwargs:\n" +
                                            entry;
                                        s_previewErr.clear();
                                    };
                                    auto fetchPreview = [&](bool force) {
                                        const double now = ImGui::GetTime();
                                        if (!force && s_previewTool == tn &&
                                            (now - s_previewAt) < 1.5)
                                            return;
                                        s_previewTool = tn;
                                        s_previewAt = now;
                                        if (!canPrev) {
                                            buildLocalPreview();
                                            s_previewErr = "Worker offline — local approximation";
                                            return;
                                        }
                                        nlohmann::json p;
                                        p["tool"] = tn;
                                        // Pass explicit trim when not inheriting.
                                        if (s_toolSchemaTrim.count(tn) &&
                                            s_toolSchemaTrim[tn] >= 0)
                                            p["trim"] = (s_toolSchemaTrim[tn] != 0);
                                        nlohmann::json r;
                                        std::string err;
                                        if (!st.worker.call("ai_tool_preview", p, r, err, 8000) ||
                                            !r.value("ok", false)) {
                                            buildLocalPreview();
                                            s_previewErr = err.empty()
                                                ? r.value("error", "preview failed")
                                                : err;
                                            return;
                                        }
                                        s_previewText = r.value("text", "");
                                        if (s_previewText.empty())
                                            s_previewText = r.value("entry", "");
                                        s_previewErr.clear();
                                    };
                                    // Refresh when popup opens / tool changes. Edits rebuild
                                    // a local approximation until Refresh is clicked.
                                    if (s_previewTool != tn) {
                                        fetchPreview(/*force=*/true);
                                    } else if (s_toolOptDirty && s_previewErr.find("offline") ==
                                               std::string::npos &&
                                               s_previewErr.find("Worker") == std::string::npos) {
                                        // Keep showing last worker text; user can Refresh.
                                    }
                                    ImGui::SameLine();
                                    if (!canPrev) ImGui::BeginDisabled();
                                    if (ImGui::SmallButton("Refresh##tool_preview"))
                                        fetchPreview(/*force=*/true);
                                    if (!canPrev) ImGui::EndDisabled();
                                    // Always refresh local approx when worker unavailable.
                                    if (!canPrev && (s_previewTool != tn || s_toolOptDirty))
                                        buildLocalPreview();
                                    if (!s_previewErr.empty()) {
                                        ImGui::PushStyleColor(ImGuiCol_Text,
                                            ui::Palette::toU32(ui::palette.statusWarn));
                                        ImGui::TextWrapped("%s", s_previewErr.c_str());
                                        ImGui::PopStyleColor();
                                    }
                                    ImGui::PushStyleColor(ImGuiCol_ChildBg,
                                        ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                                               ui::palette.panelBg[2], 1));
                                    ImGui::BeginChild("##tool_recv_preview",
                                                      ImVec2(boxW, 110 * sc), true);
                                    ImGui::PushFont(st.fonts.mono);
                                    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + boxW - 12 * sc);
                                    ImGui::TextUnformatted(
                                        s_previewText.empty() ? "(empty)" : s_previewText.c_str());
                                    ImGui::PopTextWrapPos();
                                    ImGui::PopFont();
                                    ImGui::EndChild();
                                    ImGui::PopStyleColor();

                                    ImGui::EndPopup();
                                }
                                ImGui::PopID();
                            }
                            ImGui::TreePop();
                        }

                        if (s_roleToolsDirty || s_toolOptDirty) {
                            ImGui::Dummy(ImVec2(0, 6.0f * sc));
                            if (!s_toolOptErr.empty()) {
                                ImGui::TextColored(ImVec4(1,0.4f,0.3f,1), "%s",
                                                   s_toolOptErr.c_str());
                            }
                            if (ImGui::Button("Save role tools & options",
                                              ImVec2(240.0f * sc, 0))) {
                                nlohmann::json rt = nlohmann::json::object();
                                for (const auto& kv : s_roleTools) {
                                    nlohmann::json arr = nlohmann::json::array();
                                    for (const auto& fam : kv.second) arr.push_back(fam);
                                    rt[kv.first] = std::move(arr);
                                }

                                // Derive tool_families as the union of all role assignments
                                // so legacy kill-switches / MCP external clients stay coherent.
                                nlohmann::json tf = nlohmann::json::object();
                                std::set<std::string> unionFams;
                                for (const auto& kv : s_roleTools)
                                    for (const auto& fam : kv.second) unionFams.insert(fam);
                                for (int i = 0; i < kToolFamilyCount; ++i)
                                    tf[kToolFamilies[i]] = unionFams.count(kToolFamilies[i]) > 0;

                                nlohmann::json to = nlohmann::json::object();
                                std::set<std::string> names;
                                for (const auto& kv : s_toolEnabled) names.insert(kv.first);
                                for (const auto& kv : s_toolTimeout) names.insert(kv.first);
                                for (const auto& kv : s_toolMaxCalls) names.insert(kv.first);
                                for (const auto& kv : s_toolDefaults) names.insert(kv.first);
                                for (const auto& kv : s_toolNote) names.insert(kv.first);
                                for (const auto& kv : s_toolDescOverride) names.insert(kv.first);
                                for (const auto& kv : s_toolSchemaTrim) names.insert(kv.first);
                                s_toolOptErr.clear();
                                for (const auto& tn : names) {
                                    nlohmann::json e = nlohmann::json::object();
                                    bool en = s_toolEnabled.count(tn) ? s_toolEnabled[tn] : true;
                                    if (!en) e["enabled"] = false;
                                    int tmo = s_toolTimeout.count(tn) ? s_toolTimeout[tn] : 0;
                                    if (tmo > 0) e["timeout_s"] = tmo;
                                    int mc = s_toolMaxCalls.count(tn) ? s_toolMaxCalls[tn] : 0;
                                    if (mc > 0) e["max_calls_per_turn"] = mc;
                                    if (s_toolDefaults.count(tn) && !s_toolDefaults[tn].empty()) {
                                        try {
                                            auto parsed = nlohmann::json::parse(s_toolDefaults[tn]);
                                            if (parsed.is_object() && !parsed.empty())
                                                e["param_defaults"] = parsed;
                                        } catch (...) {
                                            s_toolOptErr = "Invalid JSON in param_defaults for " + tn;
                                            break;
                                        }
                                    }
                                    if (s_toolNote.count(tn) && !s_toolNote[tn].empty())
                                        e["prompt_note"] = s_toolNote[tn];
                                    if (s_toolDescOverride.count(tn) &&
                                        !s_toolDescOverride[tn].empty())
                                        e["description_override"] = s_toolDescOverride[tn];
                                    if (s_toolSchemaTrim.count(tn) &&
                                        s_toolSchemaTrim[tn] >= 0)
                                        e["schema_trim"] = (s_toolSchemaTrim[tn] != 0);
                                    if (!e.empty()) to[tn] = e;
                                }
                                if (s_toolOptErr.empty()) {
                                    updateConfigJson([&](nlohmann::json& j) {
                                        if (!j.contains("agent") || !j["agent"].is_object())
                                            j["agent"] = nlohmann::json::object();
                                        j["agent"]["role_tools"] = rt;
                                        j["agent"]["tool_families"] = tf;
                                        j["agent"]["tool_options"] = to;
                                    });
                                    s_roleToolsDirty = false;
                                    s_toolOptDirty = false;
                                    s_roleToolsLast = -1e9;
                                }
                            }
                            ImGui::SameLine();
                            if (ImGui::Button("Reset role defaults", ImVec2(160.0f * sc, 0))) {
                                seedDefaultRoleTools();
                                s_roleToolsDirty = true;
                            }
                            ImGui::SameLine();
                            if (ImGui::Button("Reset tool options", ImVec2(150.0f * sc, 0))) {
                                s_toolEnabled.clear();
                                s_toolTimeout.clear();
                                s_toolMaxCalls.clear();
                                s_toolDefaults.clear();
                                s_toolNote.clear();
                                s_toolDescOverride.clear();
                                s_toolSchemaTrim.clear();
                                s_toolOptDirty = true;
                            }
                        }
                        settingsGlassSectionEnd();

                        ImGui::Dummy(ImVec2(0, 10.0f * sc));
                        settingsGlassSectionBegin("##ai_bugreports", "Bug reports");
                        dimText("Give the agent a submit_bug_report tool so it can file a "
                                "report when it hits tool errors or broken behavior. Reports "
                                "are markdown files in Documents/Chudware/Bug-Reports/ \xe2\x80\x94 "
                                "nothing leaves this machine.");
                        {
                            static bool s_bugReports = loadAgentConfigBool("bug_reports", true);
                            if (ImGui::Checkbox("Send bug reports", &s_bugReports))
                                saveAgentConfigValue("bug_reports", s_bugReports);
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Saved immediately to config.json "
                                                  "agent.bug_reports.");
                        }
                        settingsGlassSectionEnd();

                        ImGui::Dummy(ImVec2(0, 10.0f * sc));
                        settingsGlassSectionBegin("##ai_maintenance", "Maintenance");
                        dimText("Repair the local CAD engine runtime.");
                        {
                            const bool canSetup = !st.setupRunning.load() && !st.anyRunActive();
                            if (!canSetup) ImGui::BeginDisabled();
                            if (ImGui::Button("Repair / install runtime", ImVec2(220.0f * sc, 0)))
                                st.startSetup(true);
                            if (!canSetup) ImGui::EndDisabled();
                            if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                                ImGui::SetTooltip(
                                    "Rebuild the local Python/CAD runtime (same as "
                                    "Chudware -> Repair / install runtime).");
                            if (st.setupRunning.load()) {
                                ImGui::SameLine();
                                dimText("Running\xe2\x80\xa6");
                            }
                        }
                        settingsGlassSectionEnd();
                    }

                    // ---- Dev Preview: assembled general_agent system prompt ----
                    if (s_aiSubSel == 7) {
                        if (aiSubTabJustEntered(7)) ImGui::SetScrollY(0.0f);
                        settingsGlassSectionBegin("##ai_dev_preview", "Prompt assembly preview");
                        dimText("Shows the system prompt a typical main-chat / general_agent "
                                "turn would send (role card + capability SOFT block + harness "
                                "base + catalog + sample ledger/stage + tools slot + Auto-Learn "
                                "guidance when enabled). The coloured sample is where the user "
                                "request goes — not sent to a model.");

                        static std::string s_devSys;
                        static std::string s_devUser;
                        static std::string s_devErr;
                        static std::string s_devMeta;
                        static bool s_devLoaded = false;
                        static bool s_devFetching = false;
                        static bool s_includeTools = true;

                        const bool canDev = st.workerStarted && !busy;
                        auto fetchDevPreview = [&]() {
                            s_devFetching = true;
                            s_devErr.clear();
                            if (!canDev) {
                                s_devSys.clear();
                                s_devUser.clear();
                                s_devMeta.clear();
                                s_devErr = "Worker not ready — start the runtime to load the "
                                           "assembled prompt.";
                                s_devLoaded = true;
                                s_devFetching = false;
                                return;
                            }
                            nlohmann::json p;
                            p["include_tools"] = s_includeTools;
                            nlohmann::json r;
                            std::string err;
                            if (!st.worker.call("ai_prompt_preview", p, r, err, 30000) ||
                                !r.value("ok", false)) {
                                s_devSys.clear();
                                s_devUser.clear();
                                s_devMeta.clear();
                                s_devErr = err.empty()
                                    ? r.value("error", std::string("ai_prompt_preview failed"))
                                    : err;
                                s_devLoaded = true;
                                s_devFetching = false;
                                return;
                            }
                            s_devSys = r.value("system", "");
                            s_devUser = r.value("user_placeholder", "");
                            auto meta = r.value("meta", nlohmann::json::object());
                            char buf[256];
                            std::snprintf(buf, sizeof(buf),
                                "role=%s  tools≈%d  chars=%d  tokens≈%d  autolearn=%s",
                                meta.value("role", "general_agent").c_str(),
                                meta.value("allowed_tool_count", 0),
                                meta.value("system_chars", 0),
                                meta.value("system_tokens_est", 0),
                                meta.value("autolearn", false) ? "on" : "off");
                            s_devMeta = buf;
                            s_devErr.clear();
                            s_devLoaded = true;
                            s_devFetching = false;
                        };

                        if (!s_devLoaded && canDev && !s_devFetching)
                            fetchDevPreview();

                        if (ImGui::Checkbox("Include tools slot", &s_includeTools)) {
                            s_devLoaded = false;
                        }
                        ImGui::SameLine();
                        if (!canDev || s_devFetching) ImGui::BeginDisabled();
                        if (ImGui::Button("Refresh##dev_preview", ImVec2(100.0f * sc, 0)))
                            fetchDevPreview();
                        if (!canDev || s_devFetching) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                            ImGui::SetTooltip(canDev
                                ? "Re-assemble from live role cards + tool_schemas"
                                : "Start the worker to load the prompt preview");
                        if (!s_devMeta.empty()) {
                            ImGui::SameLine();
                            dimText(s_devMeta.c_str());
                        }
                        if (!s_devErr.empty()) {
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ui::Palette::toU32(ui::palette.statusWarn));
                            ImGui::TextWrapped("%s", s_devErr.c_str());
                            ImGui::PopStyleColor();
                        }

                        // Coloured rendering: system (dim/normal) then user placeholder (accent).
                        const float prevH = ImMax(280.0f * sc,
                            ImGui::GetContentRegionAvail().y - 8.0f * sc);
                        ImGui::PushStyleColor(ImGuiCol_ChildBg,
                            ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                                   ui::palette.panelBg[2], 1));
                        ImGui::BeginChild("##dev_prompt_body", ImVec2(-1, prevH),
                                          ImGuiChildFlags_Borders,
                                          ImGuiWindowFlags_NoScrollbar);
                        ImGui::PushFont(st.fonts.mono);
                        const float wrapX = ImGui::GetCursorPosX() +
                            ImGui::GetContentRegionAvail().x - 4.0f * sc;
                        ImGui::PushTextWrapPos(wrapX);
                        if (!s_devSys.empty()) {
                            ImGui::TextUnformatted("--- system ---");
                            ImGui::TextUnformatted(s_devSys.c_str());
                        } else if (s_devErr.empty() && !s_devLoaded) {
                            dimText("Loading…");
                        }
                        if (!s_devUser.empty()) {
                            ImGui::Dummy(ImVec2(0, 10.0f * sc));
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ui::Palette::toU32(ui::palette.accent));
                            ImGui::TextUnformatted("--- user (sample placeholder) ---");
                            ImGui::TextWrapped("%s", s_devUser.c_str());
                            ImGui::PopStyleColor();
                        }
                        ImGui::PopTextWrapPos();
                        ImGui::PopFont();
                        settingsPillScrollbar();
                        ImGui::EndChild();
                        ImGui::PopStyleColor();
                        settingsGlassSectionEnd();
                    }

                }
                settingsPillScrollbar();
                ImGui::EndChild();  // ##ai_sub_body
                ImGui::PopStyleVar();  // WindowPadding (content pane)
                s_aiSubJump = -1;
                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

// ---- Providers tab: OMP two-pane provider manager (legacy sections as the degrade path) ----
            if (ImGui::BeginTabItem("Providers", nullptr,
                                    (jumpToTab == 3) ? ImGuiTabItemFlags_SetSelected
                                                     : ImGuiTabItemFlags_None)) {
                settingsTabBodyBegin("##tab_providers");
                dimText("API providers, sign-in, and model routing. Credentials are stored by "
                        "the auth bridge \xe2\x80\x94 never written to config.json.");
                drawOmpProviderManager(st);
                settingsGlassSectionBegin("##prov_freeai", "Free AI (FreeLLMAPI)");
                if (ui::drawFreeAiProviderCard(st)) saveSettings(st.settings);
                settingsGlassSectionEnd();
                if (!s_omp.available) {
                    // Degrade path: the agent_provider_* C-ABI is absent in this build
                    // (sibling slice mid-flight / older core) — keep the legacy sections so
                    // provider config stays possible. drawOmpProviderManager drew the banner.
                    settingsGlassSectionBegin("##prov_provider", "Provider \x26 Model");
                    ui::drawAiSettingsSection(st, busy, ui::AiSettingsSection::ProviderModel);
                    settingsGlassSectionEnd();

                    settingsGlassSectionBegin("##prov_moonshot", "Moonshot (Kimi)");
                    dimText("Configured but not the active provider. Store a key to park credit; "
                            "pick a Kimi model later when you want to use it.");
                    ImGui::TextUnformatted("API key (platform.moonshot.ai)");
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::InputText("##settings_moonshot_key", st.settings.moonshotKey,
                                         sizeof(st.settings.moonshotKey),
                                         ImGuiInputTextFlags_Password)) {
                        saveSettings(st.settings);
                    }
                    {
                        const bool have = st.settings.moonshotKey[0] != '\0';
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            have ? ui::Palette::toU32(ui::palette.statusOk)
                                 : ui::Palette::toU32(ui::palette.statusWarn));
                        ImGui::TextUnformatted(have
                            ? "key set \xe2\x80\x94 Kimi models appear in the picker when enabled"
                            : "no key \xe2\x80\x94 paste a Moonshot API key when you have credit");
                        ImGui::PopStyleColor();
                    }
                    settingsGlassSectionEnd();

                    settingsGlassSectionBegin("##prov_accounts", "Accounts");
                    drawSubscriptionAccounts(st, sc);
                    settingsGlassSectionEnd();
                }

                settingsTabBodyEnd();
                ImGui::EndTabItem();
            }

            // --- end C22b marked block ---
            ImGui::EndTabBar();
        }
        ImGui::EndChild();
        popGlassChildStyle();
        ImGui::Separator();
        // Right-align the Save/Cancel pair against the modal's right gutter.
        const float footBtnsW = 120.0f * sc + 100.0f * sc + ImGui::GetStyle().ItemSpacing.x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             ImGui::GetContentRegionAvail().x - footBtnsW);
        if (primaryButton("Save", ImVec2(120.0f * sc, 34.0f * sc))) {
            settings.systemPrompt = st.spBuf.data();
            saveSettings(settings);
            st.addLog("[ok] saved settings");
            s_settingsOpen = false;
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(100.0f * sc, 34.0f * sc))) {
            // Restore the on-open snapshot so live-edited (but unsaved) fields don't linger as
            // a half-applied in-memory state (audit P1) — EXCEPT the immediate-persist
            // preferences (units / theme / glass / recent files), which already saved to
            // config.json when they changed and must keep their live values.
            Units keepUnits = settings.units;
            std::string keepTheme = settings.theme;
            ui::GlassConfig keepGlass = settings.glassCfg;
            std::string keepWs = settings.workspaceDir;   // persisted by its Apply button
            std::vector<std::string> keepRecent = settings.recentFiles;
            settings = s_snapshot;
            settings.units = keepUnits;
            settings.theme = keepTheme;
            settings.glassCfg = keepGlass;
            settings.workspaceDir = keepWs;
            settings.recentFiles = keepRecent;
            std::fill(st.spBuf.begin(), st.spBuf.end(), 0);  // discard prompt edits
            std::strncpy(st.spBuf.data(), settings.systemPrompt.c_str(), st.spBuf.size() - 1);
            s_settingsOpen = false;
        }
        }
        ImGui::End();
    }

    // ---------- About modal ----------
    if (st.showAbout) {
        ImGui::OpenPopup("About Chudware");
        st.showAbout = false;
    }
    ImGui::SetNextWindowSize(ImVec2(380.0f * sc, 0), ImGuiCond_Appearing);
    if (ImGui::BeginPopupModal("About Chudware", nullptr, ImGuiWindowFlags_NoResize)) {
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        if (wm.id) {
            ImGui::Image((ImTextureID)(intptr_t)wm.id,
                         ImVec2(190.0f * sc, 190.0f * sc * (float)wm.h / wm.w));
        } else {
            sectionHeader(st.fonts.h1, "Chudware");
        }
        dimText("Parametric CAD, driven by AI.");
        ImGui::Dummy(ImVec2(0, 4 * sc));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8 * sc));
        if (primaryButton("Close", ImVec2(110.0f * sc, 32.0f * sc))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

}  // namespace ui
