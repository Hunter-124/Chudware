// Chudware Settings modal (Appearance / General / AI / Providers / Benchmarks /
// Prompts / Agent / Experimental tabs) and the About modal.
#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "imgui_internal.h"  // FindWindowByName: clamp the saved dialog position before Begin

#include "api/chudware_version.h"  // CHUDWARE_VERSION / _GIT_SHA in the About modal

#include "settings_modal.h"
#include "tool_panel_tables.h"  // generated offline fallback tool catalog (tools/gen_tool_panel_tables.py)
#include "config_panel.h"
#include "layout.h"  // drawConfigurationsBody fallback

#include "../core/app_state.h"
#include "../core/engine_events.h"
#ifdef __EMSCRIPTEN__
#include "../core/jobs.h"
#endif
#include "file_dialog.h"
#include "../core/fs_backend.h"
#include "../core/cpp_core.h"
#include "../renderer.h"
#include "copilot/copilot.h"  // ui::drawAiSettingsSection (glass-grouped AI tab sections)
#include "ai/ai_prefs.h"  // ui::aiUiVerbosity/aiFollowActiveSession/aiActiveWorkPreviews
#include "model_registry.h"  // ui::localPresets (experimental vision-review model presets)
#include "leaderboard_ui.h"  // aicad.leaderboard/1 compact table (Settings > Benchmarks)
#include "onboarding.h"  // first-run guided provider/test/workspace flow
#ifndef __EMSCRIPTEN__
#include "../update/updater.h"
#endif
#include "web_capabilities.h"  // ui::webGapReason (sign-in is desktop-only on web)
#include "../core/async_rpc.h"  // core::RpcResult for skills/triggers RPCs
#include "widgets/interwebz.h"  // Skills/Rules two-column editors
#include "ui/provider_logos.h"  // ui::drawProviderLogo/providerBrandColor (Providers list badges)
#include "provider_usage.h"  // ui::UsageAccount/UsageWindow + THE subscription_* poller
#include "omp_descriptors.h"  // ui::parseOmp* — Omp worker payload parsing
#include "omp_session_actions.h"
#include "metrics.h"  // ui::metrics::px/stroke — DPI-scaled Settings gutter + usage rings
#include "tesseract.h"  // ui::drawTesseract + TesseractAnim — Appearance > Brand mark picker
#include "anim.h"
#include "../ui_theme.h"  // ui::busyLabel — the one inline "working" indicator
#include "widgets/kit.h"  // ui::kit::sectionHeader — the one accent-caption treatment
#include "ai/rules_authoring.h"  // ui::drawRulesAuthoringPopup (Settings → AI → Rules → New rule…)
#include "copilot/agent_text.h"  // ui::foldToAtlasGlyphs — Rules tab renders Python-authored text


// ---- C22b prompt editor state ----
// Per-role editor buffer and metadata, lazily loaded from list_editable_prompts.
namespace {
struct PromptRecord {
    std::string role;
    std::string builtinPath;     // abs path of the built-in .md
    std::string overridePath;    // abs path where we write the override
    bool overridden = false;
    // 16 KB buffer (matches the card spec)
    std::vector<char> buf{std::vector<char>(16 * 1024, 0)};
    int builtinLen = 0;  // length of the built-in text (for diff indicator)
    bool   dirty = false;      // edited since the last override write (debounced autosave pending)
    double lastEdit = 0.0;     // ImGui::GetTime() of the last keystroke
};
static std::vector<PromptRecord> s_promptRecs;
static bool s_promptsLoaded = false;
static bool s_promptsLoading = false;
static int  s_selectedRole  = 0;
static bool s_promptSystemSelected = false;
static std::string s_pendingPromptSelection;

static void selectPendingPrompt() {
    if (s_pendingPromptSelection.empty() || !s_promptsLoaded) return;
    for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
        if (s_promptRecs[i].role == s_pendingPromptSelection) {
            s_selectedRole = i;
            s_promptSystemSelected = false;
            break;
        }
    }
    s_pendingPromptSelection.clear();
}

static std::string s_promptsError;
static void applyPromptCatalog(const nlohmann::json& result) {
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
    const bool selectedSystem = s_promptSystemSelected;
    const std::string selectedRole = s_selectedRole >= 0 && s_selectedRole < (int)s_promptRecs.size()
        ? s_promptRecs[s_selectedRole].role : std::string();
    const nlohmann::json* recs = &result;
    if (result.is_object() && result.contains("prompts") && result["prompts"].is_array())
        recs = &result["prompts"];
    s_promptRecs.clear();
    if (!recs->is_array()) {
        s_selectedRole = 0;
        s_promptsLoaded = true;
        selectPendingPrompt();
        return;
    }
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
    s_selectedRole = selectedSystem ? (int)s_promptRecs.size() : 0;
    if (!selectedRole.empty()) {
        for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
            if (s_promptRecs[i].role == selectedRole) {
                s_selectedRole = i;
                break;
            }
        }
    }
    s_promptsLoaded = true;
    selectPendingPrompt();
}

static std::string s_promptSaveError;
// Writes the role's override file (creating its directory — the worker only reports the path).
// A failure is returned, never swallowed: the caller keeps the record dirty and shows `err`.
static bool savePromptOverride(PromptRecord& pr, std::string& err) {
    if (pr.overridePath.empty()) {
        err = "no override path for role " + pr.role;
        return false;
    }
    std::error_code ec;
    const std::filesystem::path path(pr.overridePath);
    if (path.has_parent_path()) std::filesystem::create_directories(path.parent_path(), ec);
    std::ofstream f(path, std::ios::out | std::ios::trunc);
    if (!f.is_open()) {
        err = "cannot write " + pr.overridePath;
        return false;
    }
    f << pr.buf.data();
    f.flush();
    if (!f) {
        err = "write failed: " + pr.overridePath;
        return false;
    }
    pr.overridden = true;
    return true;
}
// Per-role prompt editor autosave: an edit marks its record dirty and a 1 s debounce writes the
// override file; force-flushed on close, before the catalog is reloaded and at app exit.
static void flushPromptEdits(bool force) {
    for (auto& pr : s_promptRecs) {
        if (!pr.dirty) continue;
        if (!force && ImGui::GetTime() - pr.lastEdit < 1.0) continue;
        std::string err;
        if (savePromptOverride(pr, err)) {
            pr.dirty = false;
            s_promptSaveError.clear();
        } else {
            // Not retried every frame (the forced flush runs each frame while the modal is
            // closed): the error stays visible and the next keystroke tries again.
            pr.dirty = false;
            s_promptSaveError = err;
        }
    }
}

static void loadPrompts(AppState& st) {
    if (s_promptsLoading) return;
    flushPromptEdits(/*force=*/true);  // a catalog rebuild must not discard pending edits
    // list_editable_prompts is the only catalog source (YAML mission roles with overrides).
    s_promptsLoading = true;
    s_promptsError.clear();
    st.rpc.call("list_editable_prompts", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_promptsLoading = false;
        if (!r.ok) {
            s_promptsError = r.err.empty() ? "prompt catalog unavailable" : r.err;
            st.addLog("[err] prompts catalog: " + s_promptsError);
            s_promptsLoaded = false;
            return;
        }
        applyPromptCatalog(r.result);
    }, 20000);
}



static void resetPromptOverride(PromptRecord& pr, AppState& st) {
    // Delete the override file
    std::remove(pr.overridePath.c_str());
    pr.overridden = false;
    pr.dirty = false;  // the override is being deleted, not saved
    // Reload built-in text from the worker
    s_promptsLoaded = false;
    loadPrompts(st);
}

// System-prompt override (Settings > AI > Prompts > system): autosaves. Each edit marks the
// buffer dirty; a 1s debounce commits it into settings.systemPrompt and persists it, and the
// modal-closing path force-flushes so no Save button ever owns it.
static bool   s_spOverrideDirty = false;
static double s_spOverrideLastEdit = 0.0;
static void commitSystemPromptOverride(AppState& st) {
    st.settings.systemPrompt = st.spBuf.data();
    saveSettings(st.settings);
    s_spOverrideDirty = false;
}
static void flushSystemPromptOverride(AppState& st, bool force) {
    if (!s_spOverrideDirty) return;
    if (!force && ImGui::GetTime() - s_spOverrideLastEdit < 1.0) return;
    commitSystemPromptOverride(st);
}
// Every free-text editor on Settings > AI > Prompts autosaves through this: called each frame
// from drawSettingsModal (a tab switch cannot strand an edit), forced on close and at app exit.
static void flushPromptEditors(AppState& st, bool force) {
    flushSystemPromptOverride(st, force);
    flushPromptEdits(force);
}
// Skills + Rules editors (RPC-backed autosave); defined with those editors below.
static void flushSkillRuleEdits(AppState& st, bool force);
struct BoxMirrorUiState {
    bool statusInFlight = false;
    bool statusRefreshPending = false;
    bool connectInFlight = false;
    bool syncInFlight = false;
    std::string accountStatus = "Status not checked";
    std::string actionStatus;
};
static BoxMirrorUiState s_boxMirrorUi;

static std::string boxJsonString(const nlohmann::json& j, const char* key) {
    auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

static long long boxJsonInteger(const nlohmann::json& j, const char* key) {
    auto it = j.find(key);
    if (it == j.end()) return 0;
    if (it->is_number_unsigned()) return static_cast<long long>(it->get<unsigned long long>());
    return it->is_number_integer() ? it->get<long long>() : 0;
}

static std::string boxFailureText(const nlohmann::json& result,
                                  const std::string& fallback) {
    for (const char* key : {"error", "reason", "hint"}) {
        std::string text = boxJsonString(result, key);
        if (!text.empty()) return text;
    }
    return fallback;
}

static void requestBoxStatus(AppState& st) {
    if (s_boxMirrorUi.statusInFlight) {
        s_boxMirrorUi.statusRefreshPending = true;
        return;
    }
    if (!st.workerStarted) {
        s_boxMirrorUi.accountStatus = "Not connected: worker unavailable";
        return;
    }
    // The worker reads Box settings from disk, not the GUI's pending config overlay.
    flushConfigWrites();
    s_boxMirrorUi.statusInFlight = true;
    st.rpc.call("box_status", nlohmann::json{{"live", true}},
                [&st](const core::RpcResult& r) {
        auto finish = [&st]() {
            s_boxMirrorUi.statusInFlight = false;
            if (s_boxMirrorUi.statusRefreshPending) {
                s_boxMirrorUi.statusRefreshPending = false;
                requestBoxStatus(st);
            }
        };
        if (!r.ok) {
            s_boxMirrorUi.accountStatus =
                "Not connected: " + (r.err.empty() ? std::string("status unavailable") : r.err);
            finish();
            return;
        }
        const nlohmann::json& result = r.result;
        if (!result.is_object() || !result.value("ok", true)) {
            s_boxMirrorUi.accountStatus =
                "Not connected: " + boxFailureText(result, "status unavailable");
            finish();
            return;
        }
        if (!result.value("enabled", false)) {
            s_boxMirrorUi.accountStatus = "Disabled";
            finish();
            return;
        }
        if (!result.value("connected", false)) {
            s_boxMirrorUi.accountStatus =
                "Not connected: " + boxFailureText(result, "no Box account");
            finish();
            return;
        }
        std::string name;
        std::string login;
        auto account = result.find("account");
        if (account != result.end() && account->is_object()) {
            name = boxJsonString(*account, "name");
            login = boxJsonString(*account, "login");
        }
        std::string who = !name.empty() ? name : (!login.empty() ? login : "Box account");
        if (!name.empty() && !login.empty()) who += " (" + login + ")";
        s_boxMirrorUi.accountStatus =
            "Connected as " + who + " \xe2\x80\x94 " +
            std::to_string(boxJsonInteger(result, "pending")) + " pending";
        finish();
    }, 20000);
}

static void beginBoxOAuthLogin(AppState& st) {
    if (s_boxMirrorUi.connectInFlight || !st.workerStarted) return;
    flushConfigWrites();
    s_boxMirrorUi.connectInFlight = true;
    s_boxMirrorUi.actionStatus = "Starting Box sign-in\xe2\x80\xa6";
    st.rpc.call("box_oauth_login", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_boxMirrorUi.connectInFlight = false;
        if (!r.ok) {
            s_boxMirrorUi.actionStatus =
                "Box sign-in failed: " +
                (r.err.empty() ? std::string("worker unavailable") : r.err);
            requestBoxStatus(st);
            return;
        }
        const nlohmann::json& result = r.result;
        if (!result.is_object() || !result.value("ok", false)) {
            s_boxMirrorUi.actionStatus =
                "Box sign-in failed: " + boxFailureText(result, "unable to start OAuth");
            requestBoxStatus(st);
            return;
        }
        const std::string url = boxJsonString(result, "authorize_url");
        const std::string hint = boxJsonString(result, "hint");
        if (url.empty()) {
            s_boxMirrorUi.actionStatus =
                hint.empty() ? "Box sign-in did not return an authorization URL" : hint;
        } else if (!ui::openExternalHttpUrl(url)) {
            s_boxMirrorUi.actionStatus =
                "Could not open the Box authorization URL in your browser";
        } else {
            s_boxMirrorUi.actionStatus =
                hint.empty() ? "Browser opened; finish signing in to Box" : hint;
        }
        requestBoxStatus(st);
    }, 20000);
}

static void beginBoxSync(AppState& st, const std::string& projectRoot) {
    if (s_boxMirrorUi.syncInFlight || !st.workerStarted || projectRoot.empty()) return;
    flushConfigWrites();
    s_boxMirrorUi.syncInFlight = true;
    s_boxMirrorUi.actionStatus = "Syncing project to Box\xe2\x80\xa6";
    st.rpc.call("box_sync", nlohmann::json{{"path", projectRoot}},
                [&st](const core::RpcResult& r) {
        s_boxMirrorUi.syncInFlight = false;
        if (!r.ok) {
            s_boxMirrorUi.actionStatus =
                "Box sync failed: " +
                (r.err.empty() ? std::string("worker unavailable") : r.err);
            requestBoxStatus(st);
            return;
        }
        const nlohmann::json& result = r.result;
        if (!result.is_object() || !result.value("ok", false)) {
            s_boxMirrorUi.actionStatus =
                "Box sync failed: " + boxFailureText(result, "sync failed");
            requestBoxStatus(st);
            return;
        }
        long long uploaded = 0;
        long long updated = 0;
        long long unchanged = 0;
        long long skippedLarge = 0;
        auto projects = result.find("projects");
        if (projects != result.end() && projects->is_array()) {
            for (const auto& project : *projects) {
                if (!project.is_object()) continue;
                uploaded += boxJsonInteger(project, "uploaded");
                updated += boxJsonInteger(project, "updated");
                unchanged += boxJsonInteger(project, "unchanged");
                skippedLarge += boxJsonInteger(project, "skipped_large");
            }
        }
        s_boxMirrorUi.actionStatus =
            "Box sync complete: " + std::to_string(uploaded) + " uploaded, " +
            std::to_string(updated) + " updated, " + std::to_string(unchanged) +
            " unchanged, " + std::to_string(skippedLarge) + " skipped (large)";
        auto errors = result.find("errors");
        if (errors != result.end() && !errors->empty()) {
            if (errors->is_string())
                s_boxMirrorUi.actionStatus += " \xe2\x80\x94 " + errors->get<std::string>();
            else if (errors->is_array())
                s_boxMirrorUi.actionStatus +=
                    " \xe2\x80\x94 " + std::to_string(errors->size()) + " error(s)";
        }
        requestBoxStatus(st);
    }, 120000);
}

// A circled-"?" info chip that reveals a one-line explanation on hover — for discoverability
// of the less-obvious settings (vision / plan mode / max iters / units). Drawn inline
// (it does its own SameLine); the chip goes accent on hover so it reads as interactive.
//
// The chip is centred on the item it annotates, not parked at the top of the line: after a
// Checkbox or an AlignTextToFramePadding caption the line is a full frame tall, and a
// line-top chip sat superscript-high beside the label. The hit box spans from the line top to
// below the chip so the ImGui line height still covers it.
static float helpMarkerGap() { return 4.0f * ui::uiScale; }
static float helpMarkerWidth() { return ImGui::GetTextLineHeight(); }
static void helpMarker(const char* desc) {
    const float sc = ui::uiScale;
    const ImVec2 prevMin = ImGui::GetItemRectMin();
    const ImVec2 prevMax = ImGui::GetItemRectMax();
    ImGui::SameLine(0.0f, helpMarkerGap());
    const float d = helpMarkerWidth();
    const ImVec2 lineTop = ImGui::GetCursorScreenPos();
    const float cy = ImMax((prevMin.y + prevMax.y) * 0.5f, lineTop.y + d * 0.5f);
    ImGui::PushID(desc);
    ImGui::InvisibleButton("##helpchip", ImVec2(d, cy + d * 0.5f - lineTop.y));
    const bool hov = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 c(lineTop.x + d * 0.5f, cy);
    const float r = d * 0.5f - 1.0f * sc;
    const float* base = hov ? ui::palette.accent : ui::palette.textDim;
    float ring[4], glyph[4];
    ui::Palette::withAlpha(base, ring, hov ? 0.95f : 0.55f);
    ui::Palette::withAlpha(base, glyph, hov ? 1.0f : 0.75f);
    if (hov) {
        float fill[4];
        ui::Palette::withAlpha(ui::palette.accent, fill, 0.12f);
        dl->AddCircleFilled(c, r, ui::Palette::toU32(fill), 20);
    }
    dl->AddCircle(c, r, ui::Palette::toU32(ring), 20, 1.2f * sc);
    const ImVec2 ts = ImGui::CalcTextSize("?");
    dl->AddText(ImVec2(c.x - ts.x * 0.5f, c.y - ts.y * 0.5f), ui::Palette::toU32(glyph), "?");
    ImGui::PopID();
    if (hov) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 28.0f);
        ImGui::TextUnformatted(desc);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

// ---- label column for inline "caption [control]" rows ----
// A group of caption-left rows (Plan preference / Subagent preference, the budget sliders, …)
// shares ONE label column sized to its widest caption, so every control in the group starts at
// the same x. Before this each control began right after its own caption and the column read
// ragged. The column is capped so a narrow pane still leaves the control a usable width; a
// caption wider than the cap simply pushes its own control right by the minimum gap.
static float settingsLabelLeadWidth(const char* label, bool hasHelp) {
    float w = ImGui::CalcTextSize(label).x;
    if (hasHelp) w += helpMarkerGap() + helpMarkerWidth();
    return w;
}
static void settingsLabelLead(const char* label, const char* help, float columnW) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float x0 = ImGui::GetCursorScreenPos().x;
    const float cap = ImGui::GetContentRegionAvail().x * 0.55f;
    const float colW = ImMin(columnW, cap);
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(label);
    if (help && help[0]) helpMarker(help);
    const float endX = ImGui::GetItemRectMax().x;
    ImGui::SameLine(0.0f, ImMax(style.ItemSpacing.x, x0 + colW + style.ItemSpacing.x - endX));
}

// Laid-out width of a ui::kit::checkbox (box + label gap + label), for right-aligning one.
static float settingsCheckboxWidth(const char* label) {
    return ImGui::GetTextLineHeight() + ui::metrics::U(2.0f) + ImGui::CalcTextSize(label).x;
}

// Popup rows (combo drop-downs): Selectable owns input/focus/close semantics, while its header
// colors stay transparent and the current window draw list paints the wash after the item.
// Rows are sized only to the pinned popup interior so a long label can never make the popup
// grow on its next auto-fit frame.
static bool settingsPopupRowSelectable(
    const char* label, bool selected, float height = 0.0f,
    ImDrawFlags cornerRound = ImDrawFlags_RoundCornersNone) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const ImVec2 windowPos = ImGui::GetWindowPos();
    const float windowWidth = ImGui::GetWindowWidth();
    const float border = style.WindowBorderSize;
    const ImVec2 rowPos = ImGui::GetCursorScreenPos();
    // Content region, never window width: a row sized from the window edge feeds the popup's
    // own auto-fit and the popup grows a little every frame until ImGui flips it left.
    const float rowWidth = ImMax(0.0f, ImGui::GetContentRegionAvail().x);

    ImGui::PushStyleColor(ImGuiCol_Header, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign, ImVec2(0.0f, 0.5f));
    const bool clicked = ImGui::Selectable(label, selected, ImGuiSelectableFlags_None,
                                           ImVec2(rowWidth, height > 0.0f ? height :
                                                            ImGui::GetFrameHeight()));
    const bool hovered = ImGui::IsItemHovered();
    ImGui::PopStyleVar();
    ImGui::PopStyleColor(3);

    if (selected || hovered) {
        const float rowHeight = ImGui::GetItemRectSize().y;
        ImGui::GetWindowDrawList()->AddRectFilled(
            ImVec2(windowPos.x + border, rowPos.y),
            ImVec2(windowPos.x + windowWidth - border, rowPos.y + rowHeight),
            ImGui::GetColorU32(ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                                     ui::palette.accent[2], 0.30f)),
            style.PopupRounding, cornerRound);
    }
    if (hovered && label[0] != '#' && ImGui::CalcTextSize(label).x > rowWidth) {
        const ImGuiLastItemData rowItem = GImGui->LastItemData;
        ui::kit::tooltip(label);
        GImGui->LastItemData = rowItem;
    }
    return clicked;
}

// Settings list/nav rows (AI rail, role list, Skills/Rules catalogs) use the app-wide kit row —
// frosted pill, animated hover wash, selection glow — at Compact density, so a selected Settings
// row reads like every other selected row in the app instead of a flat full-bleed wash.
// `contentH` is the body height (0 = one text line); the kit adds its own padding. The hit item
// is the last item on return, so IsItemHovered / SetItemDefaultFocus / GetItemRect* keep
// working. `contentMin`/`contentW` (optional) receive the padded body origin and width for
// callers that draw extra lines under the label.
static bool settingsChildRowSelectable(const char* label, bool selected, float contentH = 0.0f,
                                       ImVec2* contentMin = nullptr,
                                       float* contentW = nullptr) {
    ui::kit::pushDensity(ui::kit::Density::Compact);
    const float padX = ui::kit::padX();
    const float padY = ui::kit::padY();
    const float bodyH = contentH > 0.0f ? contentH : ImGui::GetTextLineHeight();
    const bool clicked = ui::kit::rowBegin(label, bodyH + padY * 2.0f, selected);
    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float bodyW = ImMax(ImGui::GetItemRectMax().x - padX - origin.x,
                              ui::metrics::stroke(1.0f));
    const std::string shown(label, ImGui::FindRenderedTextEnd(label));
    const std::string fit = ui::kit::elide(shown.c_str(), bodyW);
    ImGui::GetWindowDrawList()->AddText(origin, ui::Palette::toU32(ui::palette.text),
                                        fit.c_str());
    ui::kit::rowEnd();
    ui::kit::popDensity();
    if (contentMin) *contentMin = origin;
    if (contentW) *contentW = bodyW;
    return clicked;
}

// Custom-content combo (type-to-filter box, logo rows) wearing the kit combo face: ImGui's own
// frame is drawn clear and without its stock arrow button, then the rounded control face, the
// elided preview and a vector chevron are painted on the parent draw list — the same look as
// kit::fieldEnum / kit::fieldCombo, which cannot host custom popup content. The face spans the
// caller's item width. Pin the popup width to avoid auto-fit feedback, bound its height, and let
// ImGui place it on either side of the face when there isn't enough room on the right.
static bool settingsBeginPopupRowCombo(
    const char* id, const char* preview, float widestLabel,
    ImGuiComboFlags flags = ImGuiComboFlags_None, float minPopupWidth = 0.0f) {
    const ImGuiStyle& style = ImGui::GetStyle();
    const float h = ImGui::GetFrameHeight();
    const float w = ImMax(ImGui::CalcItemWidth(), h * 3.0f);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + w, p0.y + h);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const bool hovered = ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
                         ImGui::IsMouseHoveringRect(p0, p1);
    const ImU32 fill = ImGui::GetColorU32(hovered ? ImGuiCol_FrameBgHovered : ImGuiCol_FrameBg);
    const ImU32 fillOpen = ImGui::GetColorU32(ImGuiCol_FrameBgActive);

    const ImGuiViewport* vp = ImGui::GetWindowViewport();
    const float room = ImMax(w, vp->WorkSize.x - 2.0f * style.DisplaySafeAreaPadding.x);
    const float popupW = ImMin(ImMax(ImMax(w, minPopupWidth),
                                     widestLabel + 2.0f * style.FramePadding.x +
                                         style.ScrollbarSize), room);
    // A width constraint replaces ImGui's usual combo height constraint entirely. Reapply a
    // viewport-bounded limit so long catalogs scroll instead of covering the whole display.
    const float popupH = ImMin(h * 13.0f, ImMax(h,
        vp->WorkSize.y - 2.0f * style.DisplaySafeAreaPadding.y));
    ImGui::SetNextItemWidth(w);
    ImGui::SetNextWindowSizeConstraints(ImVec2(popupW, 0.0f), ImVec2(popupW, popupH));
    // A translucent popup makes underlying table rows read as overlapping popup content.
    ImGui::SetNextWindowBgAlpha(1.0f);
    const ImVec4 clear(ui::palette.frameBg[0], ui::palette.frameBg[1], ui::palette.frameBg[2],
                       0.0f);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, clear);
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, clear);
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive, clear);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, ui::kit::rowRound());
    ImGui::PushStyleVar(ImGuiStyleVar_PopupBorderSize, ui::metrics::stroke(1.0f));
    // Nonzero popup padding is dead space the edge-to-edge row wash cannot paint over.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    const ImGuiID comboId = ImGui::GetID(id);
    const bool open = ImGui::BeginCombo(id, "", flags | ImGuiComboFlags_NoArrowButton);
    // BeginCombo does not publish an ItemInfo label of its own. Keep the real closed
    // control observable to the GUI scenario without replacing its pointer target.
    if (!open && ImGui::GetItemID() == comboId) {
        ImGuiContext& g = *GImGui;
        IMGUI_TEST_ENGINE_ITEM_INFO(comboId, id, g.LastItemData.StatusFlags);
        IMGUI_TEST_ENGINE_ITEM_INFO(comboId, preview, g.LastItemData.StatusFlags);
    }
    // Border/padding are consumed when the popup window begins. Row spacing is read live by the
    // popup content and the row wash rounds its corners with PopupRounding, so those two stay
    // pushed until settingsEndPopupRowCombo.
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(4);
    if (open) {
        ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, ui::kit::rowRound());
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0.0f, 0.0f));
    }

    // Face + preview + chevron on the PARENT draw list (captured before the popup opened).
    ui::controlFace(dl, p0, p1, open ? fillOpen : fill, ui::controlBorderU32(),
                    ui::kit::chipRound(), open ? 0.35f : 1.0f);
    const float textW = ImMax(w - style.FramePadding.x - h, ui::metrics::stroke(1.0f));
    const std::string shown = ui::kit::elide(preview ? preview : "", textW);
    const float ty = p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f;
    dl->AddText(ImVec2(p0.x + style.FramePadding.x, ty), ImGui::GetColorU32(ImGuiCol_Text),
                shown.c_str());
    const ImVec2 c(p1.x - h * 0.5f, p0.y + h * 0.5f);
    const float r = h * 0.16f;
    const ImVec2 chev[3] = {ImVec2(c.x - r, c.y - r * 0.5f), ImVec2(c.x, c.y + r * 0.5f),
                            ImVec2(c.x + r, c.y - r * 0.5f)};
    dl->AddPolyline(chev, 3, ImGui::GetColorU32(ImGuiCol_Text, 0.62f), ImDrawFlags_None,
                    ui::metrics::stroke(1.5f));
    if (!open && hovered && shown != (preview ? preview : "") && preview && preview[0])
        ui::kit::tooltip(preview);
    return open;
}

static void settingsEndPopupRowCombo() {
    ImGui::EndCombo();
    ImGui::PopStyleVar(2);  // PopupRounding, ItemSpacing
}

// Fixed-label combo: the kit combo (rounded face, vector chevron, pill rows) filling the rest of
// the current line, so it composes with settingsLabelLead captions and table cells. Unlabelled on
// purpose — the caption is the caller's. Returns true when *current changed.
static bool settingsPopupRowCombo(const char* id, int* current,
                                  const char* const* labels, int count) {
    return ui::kit::fieldEnum(id, "", current, labels, count,
                              ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
}



// Inline save/validation error ink.
static ImVec4 settingsErrInk() {
    const float* e = ui::palette.statusErr;
    return ImVec4(e[0], e[1], e[2], 1.0f);
}

// Shared Settings section chrome. A thin wrapper over the app-wide header so Settings shares the
// one accent-caption treatment (and its vertical rhythm) with every other pane; `first` suppresses
// the leading gap at the top of a tab.
static void settingsSectionHeader(const char* title, bool first = false) {
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ui::kit::sectionHeader(title, /*icon=*/nullptr, /*trailing=*/nullptr, /*count=*/-1, first);
    ui::kit::popDensity();
}

// Rounded frosted glass card around a group of related fields, under a small accent section
// header. Auto-height; id must be unique per page. Keeps label+widget pairs inside one child so
// scrolling cannot detach a caption from its field. width=0 fills the row (the default); an
// explicit width lets two sections sit side by side (Settings → Providers packs Sign-in and
// Usage into two columns on wide panes). `first` drops the header's leading gap at a page top.
static void settingsGlassSectionBegin(const char* id, const char* title, float width = 0.0f,
                                      bool first = false) {
    settingsSectionHeader(title, first);
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::px(12.0f), ui::metrics::px(8.0f)));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                        ImVec2(ui::metrics::px(8.0f), ui::metrics::px(5.0f)));
    ImGui::BeginChild(id, ImVec2(width, 0),
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
// kChatScrollbar: the same 9dp flush-right bar the copilot chat uses (the tab bodies'
// 10dp right padding is its lane, so content never runs under it).
static void settingsPillScrollbar() {
    ImVec2 mn = ImGui::GetWindowPos();
    ImVec2 mx(mn.x + ImGui::GetWindowSize().x, mn.y + ImGui::GetWindowSize().y);
    ui::drawWindowPillScrollbarV(ui::adaptivePanel(ui::panelId("settings_modal_glass"),
                                                  ImRect(mn, mx), ui::GlassGroup::Global),
                                 ui::kChatScrollbar);
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
static void settingsTabBodyBegin(const char* id, bool scroll = true,
                                 float padX = 10.0f, float padY = 8.0f) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::px(padX), ui::metrics::px(padY)));
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

// All subscription account sign-ins use the system browser. The former optional CEF path was
// deliberately removed: device-code and loopback OAuth flows are lossless externally.

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
// A rate-limit reset stamp may be ISO-8601 or a numeric epoch in milliseconds.
// ui::usageResetCountdown() reads both forms, so keep numeric values as decimal digits.
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

// Truncate with a trailing ellipsis so the rendered text fits maxW. The Providers pane used
// to let long emails / org names / model names run past the card edge and clip mid-glyph;
// every one-line label in a fixed-width cell goes through this instead.
static std::string ellipsizeToFit(const std::string& s, float maxW) {
    if (maxW <= 0.0f) return {};
    if (ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    static const char* kEll = "\xe2\x80\xa6";
    const float ellW = ImGui::CalcTextSize(kEll).x;
    if (ellW > maxW) return {};
    size_t lo = 0, hi = s.size();
    while (lo < hi) {
        const size_t mid = (lo + hi + 1) / 2;
        if (ImGui::CalcTextSize(s.c_str(), s.c_str() + mid).x + ellW <= maxW) lo = mid;
        else hi = mid - 1;
    }
    while (lo > 0 && (s[lo] & 0xC0) == 0x80) --lo;   // never split a UTF-8 continuation
    return s.substr(0, lo) + kEll;
}

// Provider list name: catalog labels carry a parenthetical qualifier ("ChatGPT Plus/Pro (Codex
// Subscription)", "Antigravity (Gemini 3, Claude, GPT-OSS)") that cut mid-word in the list pane.
// When the full label does not fit, the product name before the qualifier is shown whole (the
// caller tooltips the full label); only a name that still does not fit gets an ellipsis.
static std::string providerListLabel(const std::string& label, float maxW) {
    if (ImGui::CalcTextSize(label.c_str()).x <= maxW) return label;
    const size_t paren = label.find(" (");
    if (paren != std::string::npos && paren > 0) return ellipsizeToFit(label.substr(0, paren), maxW);
    return ellipsizeToFit(label, maxW);
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
// Cells are a FIXED width (usageWindowCellWidth) so the Usage section can flow several
// windows side by side instead of stacking one full-width row each.
static std::string usageWindowSubText(const ui::UsageWindow& w) {
    const std::string countdown = ui::usageResetCountdown(w.resetsAt);
    char sub[96];
    if (!countdown.empty())
        std::snprintf(sub, sizeof(sub), "%.0f%%  %s", w.usedFraction * 100.0f, countdown.c_str());
    else
        std::snprintf(sub, sizeof(sub), "%.0f%%", w.usedFraction * 100.0f);
    return sub;
}
static std::string usageWindowUnavailableText(const ui::UsageWindow& w) {
    const char* detail = "usage unavailable";
    if (w.status == "needs_login") detail = "sign in required";
    else if (w.status == "blocked") detail = "browser verification required";
    return w.label + " \xe2\x80\x94 " + detail;
}
static float usageWindowNaturalWidth(const ui::UsageWindow& w, float sc) {
    if (!w.hasFraction) return ImGui::CalcTextSize(usageWindowUnavailableText(w).c_str()).x;
    const float thick   = ui::metrics::stroke(3.0f);
    const float ringBox = ImGui::GetFrameHeight() * 0.42f * 2.0f + thick;
    const float textW = ImMax(ImGui::CalcTextSize(w.label.c_str()).x,
                              ImGui::CalcTextSize(usageWindowSubText(w).c_str()).x);
    return ringBox + 8.0f * sc + textW;
}
static void drawUsageWindow(const ui::UsageWindow& w, float sc, float cellW) {
    if (!w.hasFraction) {
        const std::string line = ellipsizeToFit(usageWindowUnavailableText(w), cellW);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddText(ImGui::GetCursorScreenPos(),
                    ui::Palette::toU32(ui::palette.textDim), line.c_str());
        ImGui::Dummy(ImVec2(cellW, ImGui::GetTextLineHeight()));
        return;
    }
    const float* role = ui::usageStatusColor(w.status, w.usedFraction);
    const ImU32 roleU = ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 1.0f));
    const std::string sub = usageWindowSubText(w);

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
    const float textMaxW = ImMax(0.0f, cellW - ringBox - 8.0f * sc);
    const std::string label = ellipsizeToFit(w.label, textMaxW);
    const std::string subFit = ellipsizeToFit(sub, textMaxW);

    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::drawFractionRing(dl, ImVec2(p.x + ringBox * 0.5f, p.y + rowH * 0.5f),
                         radius, thick, w.usedFraction,
                         ui::Palette::toU32(ui::palette.accentSoft), roleU);

    const float textX = p.x + ringBox + 8.0f * sc;
    const float textY = p.y + (rowH - textH) * 0.5f;
    dl->AddText(ImVec2(textX, textY), roleU, label.c_str());
    dl->AddText(ImVec2(textX, textY + lineH + lineGap),
                ui::Palette::toU32(ui::palette.textDim), subFit.c_str());
    ImGui::Dummy(ImVec2(cellW, rowH));
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
                float cellW = 0.0f;
                for (const auto& w : row.usage)
                    cellW = ImMax(cellW, usageWindowNaturalWidth(w, sc));
                const float availU = ImGui::GetContentRegionAvail().x;
                cellW = ImMin(cellW, availU);
                const float cellGap = 16.0f * sc;
                int cols = (int)((availU + cellGap) / (cellW + cellGap));
                if (cols < 1) cols = 1;
                for (size_t wi = 0; wi < row.usage.size(); ++wi) {
                    ImGui::PushID((int)wi);
                    if (wi % (size_t)cols != 0) ImGui::SameLine(0, cellGap);
                    drawUsageWindow(row.usage[wi], sc, cellW);
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

        // Live login progress for this provider (device-code / loopback system-browser UX).
        ui::UsageLoginProgress& prog = ui::providerUsageLogin()[prov];
        if (prog.inProgress) {
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
                    if (ImGui::SmallButton(openId))
                        ui::openExternalHttpUrl(fallbackUrl);
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
            // On the web (WASM) build the token store + OAuth flows live in the
            // desktop engine worker, so the button renders disabled with the gap reason
            // as its tooltip instead of firing an RPC that can only fail.
            const char* webWhy = ui::webGapReason("account_signin");
            char addId[64];
            std::snprintf(addId, sizeof(addId),
                          any ? "Add another account##%s" : "Sign in##%s", prov);
            if (webWhy) ImGui::BeginDisabled();
            if (ImGui::SmallButton(addId)) {
                prog.inProgress = true;
                prog.lastError.clear();
                prog.userCode.clear();
                prog.verifyUrl.clear();
                prog.browserOpened = false;
                prog.stage = "start";
                nlohmann::json p;
                p["provider"] = prov;
                // xAI and Kimi are device-code; Claude and Codex use loopback callbacks, and
                // Cursor uses vendor-hosted polling. Every path opens the system browser.
                if (std::string(prov) == "xai" || std::string(prov) == "kimi")
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
            if (webWhy) ImGui::EndDisabled();
            // Disabled items need AllowWhenDisabled to still show the explaining tooltip.
            if (webWhy
                    ? ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled)
                    : ImGui::IsItemHovered()) {
                if (webWhy)
                    ImGui::SetTooltip("%s", webWhy);
                else
                    ImGui::SetTooltip(
                        any
                            ? "Add another %s subscription account in your system browser."
                            : "Sign in to %s in your system browser. Existing accounts stay listed above.",
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

// ---- Omp provider manager (Settings → Providers) -------------------------------
// Two-pane sign-in manager driven by the persistent Omp worker service
// (omp_provider_list / omp_auth_state / omp_set_key / omp_remove_key /
// logout / omp_login). The UI holds no auth truth: it renders the latest sanitized
// descriptor snapshot and re-polls on open and on a short TTL.
// Field-for-field the sanitized descriptor the worker emits — parsing lives in
// omp_descriptors.cpp so host/tests can exercise the REAL rules.
using OmpProviderRow = ui::OmpProviderDesc;
struct OmpAccountRow {
    int credentialId = -1;
    std::string accountId;
    std::string email;
    std::string orgName;
};
// A credential the service itself has taken out of service (revoked token, refresh that came
// back invalid_grant, key the provider now rejects). omp_auth_state reports these per
// provider in `disabled[]`; `id` is the credential rowid and is what omp_remove_key
// wants as its `accountId`, so the panel can actually clear the dead row instead of only
// re-polling it.
struct OmpDisabledRow {
    std::string id;            // credential rowid, verbatim (omp_remove_key accountId)
    std::string cause;         // service's reason text — the only honest thing to show the user
    std::string type;          // api_key | oauth
    std::string email;         // may be absent/null
    double disabledAtMs = 0.0; // 0 = bridge has no timestamp for this row (legacy); not 1970
};
struct OmpProvDetail {
    bool loaded = false;
    std::string state;
    std::string origin;
    std::string error;
    std::vector<OmpAccountRow> accounts;
    std::vector<ui::UsageWindow> usage;
    std::vector<std::string> usageNotes;
    std::vector<OmpDisabledRow> disabled;
};
// One-call Omp OAuth login: omp_login {providerId} runs the whole flow on the
// worker. Progress arrives as STREAMED events (open_url / extension_ui_request) delivered
// through AsyncRpc's event channel on the GL thread; the terminal Done carries the final
// sanitized login state. There is no handle, no poll, and no manual code-paste method —
// Cancel is a local abandon (generation bump ignores the eventual result; the worker-side
// flow times out on its own).
struct OmpOAuthFlow {
    bool active = false;
    bool credentialSaved = false;
    bool externalOwner = false;    // onboarding/system-browser flow; independent of Settings modal
    bool browserOpenAttempted = false;
    bool browserOpened = false;
    uint64_t generation = 0;
    uint64_t beginRequest = 0;
    std::string provider;
    std::string status;        // starting|awaiting_auth|connected|error|cancelled
    std::string url;
    std::string launchUrl;
    std::string instructions;
    std::string progress;
    std::string interaction;   // browser|device (unknown values remain display-safe)
    std::string error;
    std::string localError;    // UI-side failure (e.g. browser refused to open); never from RPC
};
struct OmpProvUi {
    bool available = true;
    bool modalOpen = false;
    bool providerViewActive = false;
    bool listInFlight = false;
    double lastListPoll = -1e9;
    std::vector<OmpProviderRow> rows;
    std::string selectedId;
    std::string lastError;
    // Set when the ONE bulk omp_provider_list call failed outright. rows/detail are the
    // last known state (or empty on a cold first poll) — never "this account has no providers".
    bool listFailed = false;
    // The last omp_provider_list answer was "a running agent turn holds the service lane": a
    // wait, not a failure. rows stay as they were — or are the previous session's saved list.
    bool listDeferred = false;
    bool rowsFromSaved = false;
    // A response that arrived but carried entries this UI could not read: the readable rows are
    // live, and this names how many were dropped instead of silently shrinking the list.
    std::string listPartial;
    OmpProvDetail detail;
    std::string detailFor;
    std::string detailRequestFor;
    bool detailInFlight = false;
    double lastDetailPoll = -1e9;
    // Why the selected provider's usage/accounts are not freshly loaded ("" = nothing to say).
    std::string detailNote;
    // API-key flow (per selected provider)
    char keyBuf[256] = "";
    std::string keyFor;
    bool keyBusy = false;
    std::string keyStatus;
    bool keyStatusErr = false;
    OmpOAuthFlow oauth;
    // Separate epochs keep hidden/closed views, superseded selections and older requests
    // from accepting callbacks. nextRequestGeneration is the single monotonic allocator.
    uint64_t modalGeneration = 0;
    uint64_t viewGeneration = 0;
    uint64_t selectionGeneration = 0;
    uint64_t nextRequestGeneration = 0;
    uint64_t listRequestGeneration = 0;
    uint64_t detailRequestGeneration = 0;
    uint64_t keyRequestGeneration = 0;
    uint64_t oauthGeneration = 0;
    int lastDrawnFrame = -100;
};
static OmpProvUi s_omp;
// Bound by the error branch when no detail fetch has landed yet for the selected provider —
// s_omp.detail.disabled would then still describe a DIFFERENT provider.
static const std::vector<OmpDisabledRow> kNoDisabledRows;

static uint64_t nextOmpRequestGeneration() {
    return ++s_omp.nextRequestGeneration;
}

static bool ompViewCallbackCurrent(uint64_t modalGeneration, uint64_t viewGeneration) {
    return s_omp.modalOpen && s_omp.providerViewActive &&
           s_omp.modalGeneration == modalGeneration &&
           s_omp.viewGeneration == viewGeneration;
}

static bool ompSelectionCallbackCurrent(uint64_t modalGeneration, uint64_t viewGeneration,
                                        uint64_t selectionGeneration,
                                        const std::string& provider) {
    return ompViewCallbackCurrent(modalGeneration, viewGeneration) &&
           s_omp.selectionGeneration == selectionGeneration &&
           s_omp.selectedId == provider;
}

static void selectOmpProvider(const std::string& provider) {
    if (provider.empty() || provider == s_omp.selectedId) return;
    s_omp.selectedId = provider;
    ++s_omp.selectionGeneration;
    s_omp.detail = OmpProvDetail{};
    s_omp.detailNote.clear();
    s_omp.detailFor = provider;
    s_omp.detailRequestFor.clear();
    s_omp.detailInFlight = false;
    s_omp.lastDetailPoll = -1e9;
    s_omp.detailRequestGeneration = nextOmpRequestGeneration();
    s_omp.keyFor = provider;
    s_omp.keyBuf[0] = 0;
    s_omp.keyBusy = false;
    s_omp.keyStatus.clear();
    s_omp.keyRequestGeneration = nextOmpRequestGeneration();
}

static void enterOmpProviderView() {
    if (s_omp.providerViewActive) return;
    s_omp.providerViewActive = true;
    ++s_omp.viewGeneration;
    s_omp.available = true;
    s_omp.listInFlight = false;
    s_omp.detailInFlight = false;
    s_omp.keyBusy = false;
    s_omp.listRequestGeneration = nextOmpRequestGeneration();
    s_omp.detailRequestGeneration = nextOmpRequestGeneration();
    s_omp.keyRequestGeneration = nextOmpRequestGeneration();
    s_omp.lastListPoll = -1e9;
    s_omp.lastDetailPoll = -1e9;
}

static void leaveOmpProviderView() {
    if (!s_omp.providerViewActive) return;
    s_omp.providerViewActive = false;
    ++s_omp.viewGeneration;
    s_omp.listInFlight = false;
    s_omp.detailInFlight = false;
    s_omp.keyBusy = false;
    s_omp.listRequestGeneration = nextOmpRequestGeneration();
    s_omp.detailRequestGeneration = nextOmpRequestGeneration();
    s_omp.keyRequestGeneration = nextOmpRequestGeneration();
}

static void openOmpProviderModal() {
    ++s_omp.modalGeneration;
    s_omp.modalOpen = true;
    s_omp.available = true;
    s_omp.lastListPoll = -1e9;
    s_omp.lastDetailPoll = -1e9;
}

// Claude stack + Kimi first (design brief), then providers that need attention, then the rest
// of the signed-in ones, then everything else alpha. "error"/"disabled" outrank a healthy
// sign-in on purpose: a revoked credential is the one row in this list with an action waiting
// on it, and burying it among the signed-out providers is how it stayed invisible.
static int ompProviderRank(const OmpProviderRow& r) {
    if (r.id == "anthropic") return 0;
    if (r.id == "kimi-code") return 1;
    if (r.id == "moonshot") return 2;
    if (r.state == "error" || r.state == "disabled") return 3;
    if (r.state == "key_set" || r.state == "oauth_connected") return 4;
    return 5;
}

// providers[] -> rows in display order. An entry this UI cannot key (not an object / no id) is
// dropped from the list — that used to happen silently, i.e. one bad provider entry made a
// provider vanish with no trace — so the count comes back for the caller to report; the
// readable rows stay fully live.
static std::vector<OmpProviderRow> parseOmpProviderRows(const nlohmann::json& arr,
                                                        int& unreadable) {
    std::vector<OmpProviderRow> rows;
    rows.reserve(arr.size());
    unreadable = 0;
    for (const auto& p : arr) {
        OmpProviderRow row;
        if (!ui::parseOmpProviderDesc(p, row)) {
            ++unreadable;
            continue;
        }
        rows.push_back(std::move(row));
    }
    std::stable_sort(rows.begin(), rows.end(),
                     [](const OmpProviderRow& a, const OmpProviderRow& b) {
        const int ra = ompProviderRank(a), rb = ompProviderRank(b);
        if (ra != rb) return ra < rb;
        return a.label < b.label;
    });
    return rows;
}

static void installOmpProviderRows(std::vector<OmpProviderRow> rows) {
    bool selectedStillExists = false;
    for (const auto& row : rows)
        if (row.id == s_omp.selectedId) selectedStillExists = true;
    s_omp.rows = std::move(rows);
    if (!selectedStillExists && !s_omp.rows.empty())
        selectOmpProvider(s_omp.rows.front().id);
}

// Nothing was read live (a running turn holds the service lane, or the read failed before any
// list ever loaded): the previous session's saved provider list stands in, labelled as such, so
// the panel is not blank. It never replaces rows that were read live this session.
static void showSavedOmpProviderRows() {
    if (!s_omp.rows.empty()) return;
    const nlohmann::json saved = nlohmann::json::parse(ui::savedProviderListJson(), nullptr, false);
    if (!saved.is_array()) return;
    int unreadable = 0;
    std::vector<OmpProviderRow> rows = parseOmpProviderRows(saved, unreadable);
    if (rows.empty()) return;
    installOmpProviderRows(std::move(rows));
    s_omp.rowsFromSaved = true;
}

static void pollOmpProviderList(AppState& st, bool force) {
    if (!s_omp.providerViewActive || (s_omp.listInFlight && !force)) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_omp.lastListPoll) < 3.0) return;
    s_omp.lastListPoll = now;
    s_omp.listInFlight = true;
    const uint64_t modalGeneration = s_omp.modalGeneration;
    const uint64_t viewGeneration = s_omp.viewGeneration;
    const uint64_t requestGeneration = nextOmpRequestGeneration();
    s_omp.listRequestGeneration = requestGeneration;
    st.rpc.call("omp_provider_list", nlohmann::json::object(),
                [&st, modalGeneration, viewGeneration,
                 requestGeneration](const core::RpcResult& r) {
        if (!ompViewCallbackCurrent(modalGeneration, viewGeneration) ||
            s_omp.listRequestGeneration != requestGeneration)
            return;
        s_omp.listInFlight = false;
        // The cadence is measured from this answer, not from dispatch: a read that outlasts the
        // interval must not be asked again back to back.
        s_omp.lastListPoll = ImGui::GetTime();
        s_omp.listDeferred = core::workerLaneBusy(r);
        if (s_omp.listDeferred) {
            // hostd refuses every other call on a lane held by a running agent turn — or, for
            // the service worker, by an interactive sign-in — at once: a wait, not a failed
            // read. Keep the rows (the previous session's saved list when nothing was read
            // yet) and say when they refresh.
            const char* until = core::workerLaneBusyBySignIn(r) ? "the sign-in finishes"
                                                                 : "the current run finishes";
            s_omp.listFailed = false;
            showSavedOmpProviderRows();
            s_omp.lastError = s_omp.rowsFromSaved
                ? std::string("Showing the providers from your last session. Their status "
                              "refreshes when ") + until + "."
                : std::string("Provider status refreshes when ") + until + ".";
            return;
        }
        if (core::workerMethodAbsent(r)) {
            s_omp.available = false;
            s_omp.lastError = "Provider sign-in is not available in this runtime.";
            s_omp.listPartial.clear();
            return;
        }
        s_omp.available = true;
        // Wholesale failure of the single bulk RPC: there is no per-provider data in the
        // response to salvage, so the only honest degrade is to say the read failed, keep
        // whatever rows we last had (marked as last-known) and never let the panel imply the
        // user has no providers. The technical cause goes to the log, not onto the panel.
        auto listCallFailed = [&st, &r]() {
            std::string why = !r.ok ? r.err : std::string();
            if (why.empty())
                for (const char* key : {"reason", "error", "message"}) {
                    why = optionalJsonString(r.result, key);
                    if (!why.empty()) break;
                }
            st.addLog("[providers] the provider list could not be read: " +
                      (why.empty() ? std::string("no reason reported") : why));
            s_omp.listFailed = true;
            s_omp.listPartial.clear();
            showSavedOmpProviderRows();
            s_omp.lastError = s_omp.rows.empty()
                ? "Providers could not be loaded. It will try again automatically, or press "
                  "Retry."
                : (s_omp.rowsFromSaved
                       ? "Providers could not be refreshed; showing the list from your last "
                         "session. It will try again automatically, or press Retry."
                       : "Providers could not be refreshed; showing the last list that loaded. "
                         "It will try again automatically, or press Retry.");
        };
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !optionalJsonBool(r.result, "ok", true)) {
            listCallFailed();
            return;
        }
        const auto* arr = optionalJsonArr(r.result, "providers");
        if (!arr) {
            listCallFailed();
            return;
        }

        int unreadable = 0;
        installOmpProviderRows(parseOmpProviderRows(*arr, unreadable));
        s_omp.rowsFromSaved = false;
        s_omp.listFailed = false;
        s_omp.lastError.clear();
        s_omp.listPartial =
            unreadable == 0
                ? std::string()
                : (std::to_string(unreadable) +
                   (unreadable == 1 ? " provider entry in the response could not be read"
                                    : " provider entries in the response could not be read") +
                   " and is not listed below.");
        const std::string refreshError = optionalJsonString(r.result, "refreshError");
        if (!refreshError.empty()) {
            if (!s_omp.listPartial.empty()) s_omp.listPartial += " ";
            s_omp.listPartial += "Some provider information could not be refreshed; "
                                 "previously confirmed availability is retained.";
        }
        // Only a complete read is worth keeping for the next launch.
        if (refreshError.empty() && unreadable == 0) ui::saveProviderListJson(arr->dump());
        // A failed per-provider auth lookup is unknown, not a sign-out. Retain a previously
        // confirmed ID only when it still appears in the returned list; never admit a new
        // unknown provider, or keep an explicitly unavailable or removed provider.
        std::set<std::string> available;
        for (const auto& row : s_omp.rows)
            if (ui::ompProviderAvailableAfterRefresh(
                    row, st.signedInBridgeProviders.count(row.id) != 0))
                available.insert(row.id);
        st.signedInBridgeProviders = std::move(available);
    });
}

static void fetchOmpProviderDetail(AppState& st, const std::string& id, bool force) {
    if (!s_omp.providerViewActive || !s_omp.available || id.empty()) return;
    if (s_omp.detailInFlight && s_omp.detailRequestFor == id && !force) return;
    const double now = ImGui::GetTime();
    // A loaded detail is re-read every 60 s; one that has not loaded (the read failed, or a
    // running turn holds the service lane) is retried every 3 s — never every frame, which kept
    // the single async RPC lane saturated with doomed calls.
    const double interval = s_omp.detailFor == id && s_omp.detail.loaded ? 60.0 : 3.0;
    if (!force && s_omp.detailFor == id && (now - s_omp.lastDetailPoll) < interval)
        return;
    s_omp.lastDetailPoll = now;
    s_omp.detailInFlight = true;
    s_omp.detailFor = id;
    s_omp.detailRequestFor = id;
    const uint64_t modalGeneration = s_omp.modalGeneration;
    const uint64_t viewGeneration = s_omp.viewGeneration;
    const uint64_t selectionGeneration = s_omp.selectionGeneration;
    const uint64_t requestGeneration = nextOmpRequestGeneration();
    s_omp.detailRequestGeneration = requestGeneration;
    st.rpc.call("omp_auth_state", nlohmann::json{{"providerId", id}},
                [&st, id, modalGeneration, viewGeneration, selectionGeneration,
                 requestGeneration](const core::RpcResult& r) {
        if (!ompSelectionCallbackCurrent(modalGeneration, viewGeneration,
                                         selectionGeneration, id) ||
            s_omp.detailRequestGeneration != requestGeneration ||
            s_omp.detailRequestFor != id)
            return;
        s_omp.detailInFlight = false;
        s_omp.lastDetailPoll = ImGui::GetTime();   // retry gate runs from this answer
        if (core::workerLaneBusy(r)) {
            // A running agent turn holds the service lane. That is NOT a successful read: its
            // envelope carries no `ok`, and treating it as one wiped the accounts, usage and
            // disabled-credential rows of a signed-in provider. Keep what was last read.
            s_omp.detailNote = "Usage and accounts refresh when the current run finishes.";
            return;
        }
        if (core::workerMethodAbsent(r)) {
            s_omp.available = false;
            return;
        }
        s_omp.available = true;
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !optionalJsonBool(r.result, "ok", false)) {
            // Soft fail — keep the last detail for this exact provider and try again shortly.
            s_omp.detailNote = "Usage and accounts could not be loaded. It will try again "
                               "automatically.";
            return;
        }
        s_omp.detailNote.clear();

        OmpProvDetail d;
        d.loaded = true;
        d.state = optionalJsonString(r.result, "state");
        d.origin = optionalJsonString(r.result, "origin");
        d.error = optionalJsonString(r.result, "error");
        // auth_state's `disabled[]` — credentials the service has taken out of use. Filtered
        // per provider by the worker, so every row here belongs to `id`. `id` is a string per
        // the wire contract; read it with the stamp helper so a numeric rowid is still carried
        // through verbatim rather than silently dropped.
        if (const auto* dis = optionalJsonArr(r.result, "disabled")) {
            for (const auto& x : *dis) {
                if (!x.is_object()) continue;
                OmpDisabledRow dr;
                dr.id = optionalJsonStamp(x, "id");
                // No rowid = nothing omp_remove_key could address.
                if (dr.id.empty()) continue;
                dr.cause = optionalJsonString(x, "cause");
                dr.type = optionalJsonString(x, "type");
                dr.email = optionalJsonString(x, "email");
                auto at = x.find("disabledAtMs");
                if (at != x.end() && at->is_number()) dr.disabledAtMs = at->get<double>();
                d.disabled.push_back(std::move(dr));
            }
        }
        if (const auto* accts = optionalJsonArr(r.result, "accounts")) {
            for (const auto& a : *accts) {
                if (!a.is_object()) continue;
                OmpAccountRow ac;
                ac.credentialId = optionalJsonInt(a, "credentialId", optionalJsonInt(a, "id", 0));
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
        st.rpc.call("omp_logout_accounts", nlohmann::json{{"providerId", id}},
                    [id, modalGeneration, viewGeneration, selectionGeneration,
                     requestGeneration](const core::RpcResult& accounts) {
            if (!ompSelectionCallbackCurrent(modalGeneration, viewGeneration,
                                             selectionGeneration, id) ||
                s_omp.detailRequestGeneration != requestGeneration ||
                !accounts.ok || !accounts.result.is_object() ||
                !optionalJsonBool(accounts.result, "ok", false)) return;
            const auto* rows = optionalJsonArr(accounts.result, "accounts");
            if (!rows) return;
            std::vector<OmpAccountRow> result;
            for (const auto& row : *rows) {
                if (!row.is_object()) continue;
                OmpAccountRow account;
                account.credentialId = optionalJsonInt(row, "credentialId", -1);
                if (account.credentialId < 0) continue;
                account.accountId = optionalJsonString(row, "label");
                account.orgName = optionalJsonString(row, "detail");
                result.push_back(std::move(account));
            }
            s_omp.detail.accounts = std::move(result);
        }, 15000);
    }, 15000);   // auth_state may fetch usage reports upstream (bounded worker-side)
}

static void refreshOmpProvider(AppState& st) {
    s_omp.lastListPoll = -1e9;
    s_omp.lastDetailPoll = -1e9;
    pollOmpProviderList(st, true);
    fetchOmpProviderDetail(st, s_omp.selectedId, true);
}

// A login callback/event is current only while its flow generation is still live and its
// owner surface still exists. externalOwner flows (onboarding) survive the Settings modal.
static bool ompOAuthCallbackCurrent(uint64_t modalGeneration, uint64_t generation,
                                    const std::string& provider) {
    const OmpOAuthFlow& f = s_omp.oauth;
    const bool ownerCurrent =
        f.externalOwner ||
        (s_omp.modalOpen && s_omp.modalGeneration == modalGeneration);
    return ownerCurrent && f.generation == generation && f.provider == provider;
}

static bool openOmpOAuthUrl(const OmpOAuthFlow& f);

static void openOmpOAuthInSystemBrowser(OmpOAuthFlow& f) {
    if (f.status != "awaiting_auth" || f.browserOpenAttempted ||
        (f.url.empty() && f.launchUrl.empty()))
        return;
    f.browserOpenAttempted = true;
    f.browserOpened = openOmpOAuthUrl(f);
    if (!f.browserOpened)
        f.localError = "The system browser could not be opened. Use the link below.";
}

static void failOmpOAuth(OmpOAuthFlow& f, std::string error) {
    f.active = false;
    f.status = "error";
    f.error = error.empty() ? "Sign-in failed" : std::move(error);
}

// One streamed omp_login call runs the whole OAuth flow on the Omp worker: the
// worker opens/settles the provider flow itself and streams open_url / instruction events
// while this call is in flight; the terminal result is the final sanitized login state.
// There is no handle to poll and no manual code RPC — the loopback/device exchange is
// worker-side. Cancel is a local abandon (generation bump); the worker flow times out alone.
static void beginOmpOAuth(AppState& st, const std::string& provider,
                          bool externalOwner = false) {
    OmpOAuthFlow& f = s_omp.oauth;
    if (f.active || provider.empty()) return;
    f = OmpOAuthFlow{};
    f.active = true;
    f.externalOwner = externalOwner;
    f.provider = provider;
    f.status = "starting";
    f.generation = ++s_omp.oauthGeneration;
    const uint64_t modalGeneration = s_omp.modalGeneration;
    const uint64_t generation = f.generation;
    const uint64_t requestGeneration = nextOmpRequestGeneration();
    f.beginRequest = requestGeneration;
    // Its own queue (st.authRpc): the flow is one call that outlasts the user's browser step,
    // and on the shared FIFO it stalled every provider poll and document call behind it.
    st.authRpc.call(
        "omp_login", nlohmann::json{{"providerId", provider}},
        [&st, provider, modalGeneration, generation,
         requestGeneration](const core::RpcResult& r) {
            if (!ompOAuthCallbackCurrent(modalGeneration, generation, provider) ||
                s_omp.oauth.beginRequest != requestGeneration)
                return;   // superseded/cancelled — the late result is simply dropped
            OmpOAuthFlow& f = s_omp.oauth;
            if (core::workerLaneBusy(r)) {
                // A running agent turn holds the service lane: say when sign-in comes back
                // instead of the generic failure.
                failOmpOAuth(f, "Sign-in is available when the current run finishes.");
                return;
            }
            if (!r.ok || !r.result.is_object() ||
                !optionalJsonBool(r.result, "ok", false)) {
                std::string error = !r.ok ? r.err : optionalJsonString(r.result, "error");
                if (error.empty() && r.result.is_object())
                    error = optionalJsonString(r.result, "reason");
                if (error.empty() && r.result.is_object())
                    error = optionalJsonString(r.result, "detail");
                failOmpOAuth(f, error.empty() ? "sign-in failed" : error);
                return;
            }
            // Terminal state: the worker only returns after the flow settled. A usable
            // state (or an explicit connected marker) is success; anything else is the
            // provider's refusal, reported verbatim.
            const std::string state = optionalJsonString(r.result, "state");
            const bool connected = ui::ompProviderUsable(state) ||
                                   optionalJsonBool(r.result, "connected", false) ||
                                   optionalJsonBool(r.result, "authenticated", false);
            if (connected) {
                f.status = "connected";
                f.credentialSaved = true;
                f.active = false;
                st.addLog("[ok] signed in to " + f.provider);
                refreshOmpProvider(st);
            } else {
                failOmpOAuth(f, optionalJsonString(r.result, "error"));
            }
        },
        // Streamed mid-call progress (GL thread via AsyncRpc::drain): open_url hands the
        // browser URL over; other kinds carry display text. Snapshot semantics per event.
        [provider, modalGeneration, generation,
         requestGeneration](const nlohmann::json& ev) {
            if (!ompOAuthCallbackCurrent(modalGeneration, generation, provider) ||
                s_omp.oauth.beginRequest != requestGeneration)
                return;
            ui::OmpLoginEvent e;
            if (!ui::parseOmpLoginEvent(ev, e)) return;
            OmpOAuthFlow& f = s_omp.oauth;
            if (e.kind == "open_url") {
                f.url = e.url;
                f.launchUrl = e.launchUrl;
                if (!e.message.empty()) f.instructions = e.message;
                if (!e.interaction.empty()) f.interaction = e.interaction;
                f.status = "awaiting_auth";
                openOmpOAuthInSystemBrowser(f);
            } else if (!e.message.empty()) {
                f.progress = e.message;
                if (f.status == "starting") f.status = "awaiting_auth";
            }
        },
        // The whole flow rides ONE call: give the user time to finish in the browser.
        300000);
}

static void cancelOmpOAuth(AppState&) {
    OmpOAuthFlow& f = s_omp.oauth;
    // Local abandon: bump the generation so the in-flight call's event/done callbacks are
    // ignored. The worker-side flow is not addressable (no handle) and expires on its own.
    f.generation = ++s_omp.oauthGeneration;
    f.active = false;
    f.status = "cancelled";
    f.error.clear();
    f.localError.clear();
}

static bool openOmpOAuthUrl(const OmpOAuthFlow& f) {
#if defined(__EMSCRIPTEN__)
    return ui::openExternalHttpUrl(f.url);
#else
    if (!f.launchUrl.empty() && ui::openExternalHttpUrl(f.launchUrl)) return true;
    return ui::openExternalHttpUrl(f.url);
#endif
}

static void closeOmpProviderModal(AppState& st) {
    leaveOmpProviderView();
    if (s_omp.oauth.active) cancelOmpOAuth(st);
    ++s_omp.modalGeneration;
    s_omp.modalOpen = false;
}

static const float* ompStateColor(const std::string& state) {
    if (state == "oauth_connected" || state == "key_set" || state == "configured") return ui::palette.statusOk;
    if (state == "error") return ui::palette.statusErr;
    return ui::palette.textDim;   // signed_out / disabled / unknown
}

static const char* ompStateLabel(const OmpProviderRow& r) {
    if (r.state == "oauth_connected") return "OAuth connected";
    if (r.state == "key_set")
        return r.origin == "env" ? "key set (env)" : "API key set";
    if (r.state == "configured") return "configured";
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

// Remove ONE credential row from the Omp service (accountId = its rowid, as reported by
// accounts[].credentialId or disabled[].id), or every credential for the provider when
// accountId is empty. A disabled row is removable through the exact same omp_remove_key
// call as a live key, so no extra method is involved.
static void removeOmpCredential(AppState& st, const std::string& provider,
                                const std::string& account) {
    nlohmann::json params{{"providerId", provider}};
    if (!account.empty()) params["accountId"] = account;  // omitted = all rows for the provider
    const uint64_t modalGeneration = s_omp.modalGeneration;
    const uint64_t viewGeneration = s_omp.viewGeneration;
    const uint64_t selectionGeneration = s_omp.selectionGeneration;
    const uint64_t requestGeneration = nextOmpRequestGeneration();
    s_omp.keyRequestGeneration = requestGeneration;
    st.rpc.call("omp_remove_key", std::move(params),
                [&st, provider, modalGeneration, viewGeneration, selectionGeneration,
                 requestGeneration](const core::RpcResult& r) {
        if (!ompSelectionCallbackCurrent(modalGeneration, viewGeneration,
                                         selectionGeneration, provider) ||
            s_omp.keyRequestGeneration != requestGeneration ||
            s_omp.keyFor != provider)
            return;
        if (core::workerLaneBusy(r)) {
            // A running agent turn holds the service lane: the credential was NOT removed, and
            // the envelope carries no `ok` (reading that as success hid the refusal).
            s_omp.lastError = "Removing a credential is available when the current run finishes.";
        } else if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
                   !optionalJsonBool(r.result, "ok", false)) {
            std::string why = !r.ok ? r.err : optionalJsonString(r.result, "error");
            if (why.empty()) why = optionalJsonString(r.result, "reason");
            s_omp.lastError =
                why.empty() ? "The credential could not be removed. Try again." : why;
        } else {
            s_omp.lastError.clear();
        }
        // Either way the stored state may have moved: re-read it rather than guessing.
        refreshOmpProvider(st);
    });
}

// "2026-08-27 14:05" in local time. The bridge sends 0 for rows whose disable timestamp it
// never recorded, and 0 must read as unknown — printing the epoch would be a wrong number
// presented as fact.
static std::string ompDisabledWhen(double ms) {
    if (!(ms > 0.0)) return {};
    const std::time_t t = (std::time_t)(ms / 1000.0);
    const std::tm* lt = std::localtime(&t);
    if (!lt) return {};
    char buf[32];
    if (std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M", lt) == 0) return {};
    return buf;
}

// What the provider list shows when it has no rows to paint. It must never describe a failed or
// deferred read as "you have no providers", and a filter that matches nothing is not a load.
// Both panes share this.
static const char* ompEmptyListMessage() {
    if (!s_omp.rows.empty()) return "No providers match your search.";
    if (s_omp.listInFlight) return "Loading providers\xe2\x80\xa6";
    if (s_omp.listDeferred) return "Providers appear when the current run finishes.";
    if (s_omp.listFailed)
        return "Providers could not be loaded. It will try again automatically, or press Retry.";
    return "No providers are available.";
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
    // Measured once at the top: decides the two-column Sign-in/Usage split below.
    const float paneW = ImGui::GetContentRegionAvail().x;
    OmpOAuthFlow& flow = s_omp.oauth;
    const bool hasUsage = detailReady && !s_omp.detail.usage.empty();

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
    const float stateX = ImGui::GetCursorPosX();   // on-line: valid
    const float stateW = ImGui::CalcTextSize(ompStateLabel(prov)).x;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(scol[0], scol[1], scol[2], 1.0f));
    ImGui::TextUnformatted(ompStateLabel(prov));
    ImGui::PopStyleColor();
    // Disable = CAD's provider-category off switch (composer picker hides the models;
    // credentials stay). Only the bridge providers with a CAD category key get the toggle.
    // Right-aligned so the header line carries it instead of a second mostly-empty row.
    const char* catKey = nullptr;
    if (prov.id == "anthropic") catKey = "anthropic";
    else if (prov.id == "kimi-code") catKey = "kimi-code";
    else if (prov.id == "moonshot") catKey = "moonshot";
    if (catKey) {
        const float cbW = settingsCheckboxWidth("Disable");
        // Cursor has wrapped to the next line start after the state text, so line-start +
        // avail IS the right edge — but the fit guard must compare against the measured
        // state-text end (stateX + stateW), not the wrapped cursor.
        const float rightEdge = ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x;
        const float headerEnd = stateX + stateW;
        const float rx = rightEdge - cbW;
        if (rx >= headerEnd + 12.0f * sc) ImGui::SameLine(rx, 0);
        else if (headerEnd + 16.0f * sc + cbW <= rightEdge) ImGui::SameLine(0, 16.0f * sc);
        // else: too narrow even inline — the checkbox wraps to its own line.
        bool disabled = st.settings.disabledProviders.count(catKey) != 0;
        if (ui::kit::checkbox("prov_disable", "Disable", &disabled)) {
            if (disabled) st.settings.disabledProviders.insert(catKey);
            else st.settings.disabledProviders.erase(catKey);
            saveSettings(st.settings);
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Hide this provider's models from the copilot picker. "
                              "Credentials are kept — re-enable restores.");
    }
    // Why usage and accounts are not freshly loaded (a run holds the service lane, or the read
    // failed and is being retried) — said plainly instead of leaving the sections quietly empty.
    if (!s_omp.detailNote.empty() && s_omp.detailFor == prov.id) dimText(s_omp.detailNote.c_str());

    // Inline "Sign in…" primary button — sits on the status line of whichever state branch
    // is active instead of eating its own row below it.
    auto signInSameLine = [&]() {
        if (!prov.hasOAuth || flow.active) return;
        ImGui::SameLine(0, 12.0f * sc);
        if (primaryButton("Sign in\xe2\x80\xa6", ImVec2(140.0f * sc, 0)))
            beginOmpOAuth(st, prov.id);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Browser OAuth (PKCE loopback or device code, whichever the "
                              "provider supports). The agent service runs the whole "
                              "exchange; this panel opens the approval URL and reports "
                              "the final state.");
    };

    // ---- Sign-in + Usage sections: two columns on wide panes, stacked on narrow ones ----
    const float secGap = 10.0f * sc;
    const bool twoCol = hasUsage && paneW >= 560.0f * sc;
    const float colW = twoCol ? (paneW - secGap) * 0.5f : 0.0f;
    const float secX = ImGui::GetCursorPosX();
    const float secY = ImGui::GetCursorPosY();

    // ---- Sign-in section ----
    settingsGlassSectionBegin("##omp_signin", "Sign-in", colW);
    if (state == "error") {
        // The cause is the whole point of this branch: a credential the bridge has rejected or
        // taken out of service. Prefer the per-provider detail error, fall back to the list
        // row's, and when neither is populated say that plainly instead of rendering a blank
        // red line that looks like a rendering bug.
        std::string cause = detailReady && !s_omp.detail.error.empty()
                                ? s_omp.detail.error : prov.error;
        const std::vector<OmpDisabledRow>& dead =
            detailReady ? s_omp.detail.disabled : kNoDisabledRows;
        if (cause.empty() && !dead.empty()) cause = dead.front().cause;
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
        ImGui::TextWrapped("%s", cause.empty()
                                     ? "This provider's stored credential is not usable and the "
                                       "bridge did not report a reason."
                                     : cause.c_str());
        ImGui::PopStyleColor();
        // One row per credential the bridge disabled, each with the Remove that clears exactly
        // that rowid. Retrying a revoked token can never succeed — removing it is the only
        // action that changes anything, so it has to exist here.
        for (const auto& dr : dead) {
            ImGui::PushID(dr.id.c_str());
            std::string who = !dr.email.empty() ? dr.email : ("credential #" + dr.id);
            if (!dr.type.empty()) who += " (" + dr.type + ")";
            const std::string when = ompDisabledWhen(dr.disabledAtMs);
            std::string line = who + " \xe2\x80\x94 disabled";
            if (!when.empty()) line += " " + when;
            if (!dr.cause.empty()) line += ": " + dr.cause;
            ImGui::Bullet();
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted(
                ellipsizeToFit(line, ImGui::GetContentRegionAvail().x - 80.0f * sc).c_str());
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 8.0f * sc);
            if (ImGui::SmallButton("Remove")) removeOmpCredential(st, prov.id, dr.id);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Delete this dead credential from the auth bridge, so the "
                                  "provider stops reporting an error and you can sign in again.");
            ImGui::PopID();
        }
        if (ImGui::SmallButton("Retry")) refreshOmpProvider(st);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Re-read this provider's state from the auth bridge. This does not "
                              "clear anything \xe2\x80\x94 a revoked credential stays revoked.");
        if (dead.empty()) {
            // The bridge reports an unusable credential but named no removable row, so the only
            // handle we have is the provider-wide removal the same RPC supports.
            ImGui::SameLine(0, 8.0f * sc);
            if (ImGui::SmallButton("Remove credentials"))
                removeOmpCredential(st, prov.id, std::string());
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("The bridge did not name a specific credential row, so this "
                                  "removes EVERY stored credential for this provider.");
        }
        ImGui::SameLine(0, 8.0f * sc);
    }
    if (state == "oauth_connected") {
        ompStatusDotText("OAuth connected", pal.statusOk, /*filled=*/true);
        if (prov.hasOAuth && !flow.active) {
            ImGui::SameLine(0, 12.0f * sc);
            if (ImGui::SmallButton("Add account")) beginOmpOAuth(st, prov.id);
        }
        // Logout is credential-specific. Never guess an account id from a provider id.
        if (detailReady) {
            // One line per account: email + org, each ellipsized to the space it actually
            // has — the old layout let "email  org" run past the card edge and clip mid-word.
            for (const auto& ac : s_omp.detail.accounts) {
                ImGui::PushID(ac.credentialId);
                std::string who = !ac.email.empty() ? ac.email
                                  : (!ac.accountId.empty() ? shortAccountId(ac.accountId)
                                                           : "(account)");
                ImGui::Bullet();
                if (ac.orgName.empty()) {
                    ImGui::TextUnformatted(
                        ellipsizeToFit(who, ImGui::GetContentRegionAvail().x).c_str());
                } else {
                    ImGui::TextUnformatted(
                        ellipsizeToFit(who, ImGui::GetContentRegionAvail().x * 0.52f).c_str());
                    ImGui::SameLine(0, 6.0f * sc);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                    ImGui::TextUnformatted(
                        ellipsizeToFit(ac.orgName, ImGui::GetContentRegionAvail().x).c_str());
                    ImGui::PopStyleColor();
                }
                if (ac.credentialId >= 0) {
                    ImGui::SameLine(0, 6.0f * sc);
                    if (ImGui::SmallButton("Sign out")) {
                        const std::string provider = prov.id;
                        const uint64_t modalGeneration = s_omp.modalGeneration;
                        const uint64_t viewGeneration = s_omp.viewGeneration;
                        const uint64_t selectionGeneration = s_omp.selectionGeneration;
                        st.rpc.call("omp_logout",
                            nlohmann::json{{"providerId", provider}, {"credentialId", ac.credentialId}},
                            [&st, provider, modalGeneration, viewGeneration,
                             selectionGeneration](const core::RpcResult& r) {
                                if (!ompSelectionCallbackCurrent(modalGeneration, viewGeneration,
                                                                 selectionGeneration, provider)) return;
                                if (core::workerLaneBusy(r))
                                    s_omp.lastError = "Signing out is available when the current "
                                                      "run finishes.";
                                else if (!r.ok || !r.result.is_object() ||
                                    !optionalJsonBool(r.result, "ok", false))
                                    s_omp.lastError = !r.ok ? r.err : optionalJsonString(r.result, "reason");
                                else s_omp.lastError.clear();
                                refreshOmpProvider(st);
                            });
                    }
                }
                ImGui::PopID();
            }
        }
    } else if (state == "key_set") {
        ompStatusDotText(prov.origin == "env" && !prov.envVar.empty()
                             ? (std::string("key set via ") + prov.envVar).c_str()
                             : "API key set",
                         pal.statusOk, /*filled=*/true);
        ImGui::SameLine(0, 12.0f * sc);
        if (ImGui::SmallButton("Remove key"))
            removeOmpCredential(st, prov.id, std::string());
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Remove the stored key from the auth bridge. "
                              "Paste a new key below to replace it.");
        signInSameLine();   // key_set providers that also support OAuth keep the entry point
    } else if (state == "configured") {
        ompStatusDotText("Credential configured", pal.statusOk, /*filled=*/true);
        signInSameLine();
    } else {
        ompStatusDotText("signed out", pal.textDim, /*filled=*/false);
        signInSameLine();
    }
    if ((state == "signed_out" || prov.keyless) && prov.modelsAvailable > 0) {
        dimText(prov.keyless ? "This provider can serve models without a credential."
                             : "Credential-free access is not confirmed for this provider.");
        bool enabled = false;
        try {
            const auto config = readConfigJson();
            const auto agent = config.value("agent", nlohmann::json::object());
            const auto allowed = agent.value("keyless_providers", nlohmann::json::array());
            if (allowed.is_array())
                for (const auto& id : allowed)
                    if (id.is_string() && id.get<std::string>() == prov.id) enabled = true;
        } catch (...) {}
        if (ui::kit::checkbox("keyless_opt_in", "Allow without a stored credential", &enabled)) {
            const std::string provider = prov.id;
            const bool saved = updateConfigJson([&](nlohmann::json& config) {
                auto& agent = config["agent"];
                if (!agent.is_object()) agent = nlohmann::json::object();
                auto& allowed = agent["keyless_providers"];
                if (!allowed.is_array()) allowed = nlohmann::json::array();
                nlohmann::json next = nlohmann::json::array();
                for (const auto& id : allowed)
                    if (!id.is_string() || id.get<std::string>() != provider) next.push_back(id);
                if (enabled) next.push_back(provider);
                allowed = std::move(next);
            });
            // The provider list is computed by the worker from config.json on disk, so the write
            // must land before it is re-queried; a failed write is reported, not shown as saved.
            if (!saved || !flushConfigWrites())
                s_omp.lastError = "Could not save the credential-free access setting.";
            refreshOmpProvider(st);
        }
    }

    const bool flowVisible = flow.provider == prov.id &&
                             (flow.active || flow.status == "error" ||
                              flow.status == "cancelled");
    if (flowVisible) {
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (flow.status == "starting") {
            ImGui::TextUnformatted("Starting sign-in\xe2\x80\xa6");
            ImGui::SameLine(0, 8.0f * sc);
            if (ImGui::SmallButton("Cancel")) cancelOmpOAuth(st);
        } else if (flow.status == "awaiting_auth") {
            if (!flow.progress.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.progress.c_str());
                ImGui::PopStyleColor();
            } else {
                ImGui::TextUnformatted(flow.interaction == "device"
                                           ? "Waiting for device approval\xe2\x80\xa6"
                                           : "Waiting for browser approval\xe2\x80\xa6");
            }
            if (!flow.instructions.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", flow.instructions.c_str());
                ImGui::PopStyleColor();
            }
            // The raw URL is the only browser-safe target. Native may use launchUrl through
            // the same argv-safe platform opener, then falls back to the raw URL.
            const std::string& visibleUrl = !flow.url.empty() ? flow.url : flow.launchUrl;
            if (!visibleUrl.empty()) {
                ImGui::TextWrapped("%s", visibleUrl.c_str());
                if (ImGui::SmallButton("Copy URL"))
                    ImGui::SetClipboardText(visibleUrl.c_str());
                ImGui::SameLine(0, 6.0f * sc);
                if (ImGui::SmallButton("Open in browser") && !openOmpOAuthUrl(flow))
                    flow.localError = "This sign-in URL could not be opened safely.";
            }
            if (ImGui::SmallButton("Cancel")) cancelOmpOAuth(st);
            const std::string& visibleError =
                !flow.localError.empty() ? flow.localError : flow.error;
            if (!visibleError.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
                ImGui::TextWrapped("%s", visibleError.c_str());
                ImGui::PopStyleColor();
            }
        } else if (flow.status == "error") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusErr));
            ImGui::TextWrapped("%s", flow.error.empty() ? "Sign-in failed" : flow.error.c_str());
            ImGui::PopStyleColor();
            if (prov.hasOAuth && ImGui::SmallButton("Try again"))
                beginOmpOAuth(st, prov.id);
        } else if (flow.status == "cancelled") {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
            ImGui::TextUnformatted("Sign-in canceled.");
            ImGui::PopStyleColor();
            if (prov.hasOAuth && ImGui::SmallButton("Try again"))
                beginOmpOAuth(st, prov.id);
        }
    }

    // Store the key through the runtime; storage does not imply provider validation.
    if (prov.hasApiKey || ImGui::CollapsingHeader("Add API key (provider support unconfirmed)")) {
        if (!prov.hasApiKey)
            dimText("A saved key does not establish that this provider accepts API-key authentication.");
        ImGui::Dummy(ImVec2(0, 4.0f * sc));
        if (s_omp.keyFor != prov.id) {
            s_omp.keyFor = prov.id;
            s_omp.keyBuf[0] = 0;
            s_omp.keyBusy = false;
            s_omp.keyStatus.clear();
            s_omp.keyRequestGeneration = nextOmpRequestGeneration();
        }
        ImGui::TextUnformatted(state == "key_set" ? "Replace API key" : "Add API key");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##omp_key", "Paste key\xe2\x80\xa6", s_omp.keyBuf,
                                 sizeof(s_omp.keyBuf), ImGuiInputTextFlags_Password);
        const bool haveInput = s_omp.keyBuf[0] != 0;
        if (s_omp.keyBusy) ImGui::BeginDisabled();
        if (ImGui::SmallButton("Add key") && haveInput && !s_omp.keyBusy) {
            s_omp.keyBusy = true;
            s_omp.keyStatus.clear();
            const std::string provider = prov.id;
            const std::string key = s_omp.keyBuf;
            const uint64_t modalGeneration = s_omp.modalGeneration;
            const uint64_t viewGeneration = s_omp.viewGeneration;
            const uint64_t selectionGeneration = s_omp.selectionGeneration;
            const uint64_t requestGeneration = nextOmpRequestGeneration();
            s_omp.keyRequestGeneration = requestGeneration;
            st.rpc.call("omp_set_key",
                        nlohmann::json{{"providerId", provider}, {"apiKey", key}},
                        [&st, provider, modalGeneration, viewGeneration,
                         selectionGeneration, requestGeneration](const core::RpcResult& sr) {
                if (!ompSelectionCallbackCurrent(modalGeneration, viewGeneration,
                                                 selectionGeneration, provider) ||
                    s_omp.keyRequestGeneration != requestGeneration ||
                    s_omp.keyFor != provider) return;
                s_omp.keyBusy = false;
                if (core::workerLaneBusy(sr)) {
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = "Saving a key is available when the current run finishes.";
                    return;
                }
                if (!sr.ok || !sr.result.is_object() ||
                    !optionalJsonBool(sr.result, "ok", false)) {
                    s_omp.keyStatusErr = true;
                    s_omp.keyStatus = !sr.ok ? sr.err : optionalJsonString(sr.result, "error");
                    if (s_omp.keyStatus.empty()) s_omp.keyStatus = "Could not save key.";
                    return;
                }
                s_omp.keyStatusErr = false;
                s_omp.keyStatus = "Key saved; provider access is verified when used.";
                s_omp.keyBuf[0] = 0;
                refreshOmpProvider(st);
            }, 20000);
        }
        if (s_omp.keyBusy) ImGui::EndDisabled();
        if (!prov.consoleUrl.empty()) {
            ImGui::SameLine(0, 6.0f * sc);
            if (ImGui::SmallButton("Get a key"))
                ui::openExternalHttpUrl(prov.consoleUrl);
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", prov.consoleUrl.c_str());
        }
        if (!s_omp.keyStatus.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ui::Palette::toU32(s_omp.keyStatusErr ? pal.statusErr : pal.statusOk));
            ImGui::TextWrapped("%s", s_omp.keyStatus.c_str());
            ImGui::PopStyleColor();
        }
    }
    settingsGlassSectionEnd();

    // ---- Usage section (OAuth subscription windows + observed-spend notes) ----
    // Gauges flow side by side in fixed-width cells (one full-width row each before); on wide
    // panes the whole card sits in the right column next to Sign-in.
    if (hasUsage) {
        float yAfterSignin = 0.0f;
        if (twoCol) {
            yAfterSignin = ImGui::GetCursorPosY();
            // The second column must be ONE item that honors the cursor position: a
            // zero-padding wrapper child. Drawing the section straight after SetCursorPos
            // only places the header's spacer Dummy — the title and card then wrap back to
            // the left margin and the two cards overlap.
            ImGui::SetCursorPos(ImVec2(secX + colW + secGap, secY));
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
            ImGui::BeginChild("##omp_usage_col", ImVec2(colW, 0),
                              ImGuiChildFlags_AutoResizeY);
            ImGui::PopStyleVar();
        }
        settingsGlassSectionBegin("##omp_usage", "Usage");
        float cellW = 0.0f;
        for (const auto& w : s_omp.detail.usage)
            cellW = ImMax(cellW, usageWindowNaturalWidth(w, sc));
        const float availU = ImGui::GetContentRegionAvail().x;
        cellW = ImMin(cellW, availU);
        const float cellGap = 16.0f * sc;
        int usageCols = (int)((availU + cellGap) / (cellW + cellGap));
        if (usageCols < 1) usageCols = 1;
        for (size_t i = 0; i < s_omp.detail.usage.size(); ++i) {
            if (i % (size_t)usageCols != 0) ImGui::SameLine(0, cellGap);
            drawUsageWindow(s_omp.detail.usage[i], sc, cellW);
        }
        for (const auto& n : s_omp.detail.usageNotes) dimText(n.c_str());
        settingsGlassSectionEnd();
        if (twoCol) {
            ImGui::EndChild();
            ImGui::SetCursorPosY(ImMax(yAfterSignin, ImGui::GetCursorPosY()));
        }
    }

    // ---- Models section (catalog capability chips + ctx + $in/$out per Mtok) ----
    {
        int count = 0;
        for (const auto& m : ui::bridgeModelCatalog())
            if (m.provider == prov.id && !ui::isLegacyGenerationModel(m.id)) ++count;
        char modelsTitle[64];
        std::snprintf(modelsTitle, sizeof(modelsTitle), "Models (%d)",
                      count > 0 ? count : prov.modelsAvailable);
        settingsGlassSectionBegin("##omp_models", modelsTitle);

        static char s_modelSearchBuf[64] = "";
        if (count > 0) {
            if (ImGui::SmallButton("Select all")) {
                for (const auto& m : ui::bridgeModelCatalog())
                    if (m.provider == prov.id && !ui::isLegacyGenerationModel(m.id)) {
                        const std::string identity = m.provider + "/" + m.id;
                        st.settings.hiddenModels.erase(identity);
                        st.settings.shownLegacyHiddenModels.insert(identity);
                    }
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show every catalog model for this provider in the "
                                  "copilot model picker.");
            ImGui::SameLine(0, 6.0f * sc);
            if (ImGui::SmallButton("Clear")) {
                for (const auto& m : ui::bridgeModelCatalog())
                    if (m.provider == prov.id && !ui::isLegacyGenerationModel(m.id)) {
                        const std::string identity = m.provider + "/" + m.id;
                        st.settings.hiddenModels.insert(identity);
                        st.settings.shownLegacyHiddenModels.erase(identity);
                    }
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Hide every catalog model for this provider from the "
                                  "copilot model picker.");
            ImGui::SameLine(0, 8.0f * sc);
        }
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##model_search", "Search models...", s_modelSearchBuf,
                                 sizeof(s_modelSearchBuf));
        const std::string modelNeedle = toLowerAscii(s_modelSearchBuf);

        if (count == 0) {
            // Say which of the four things is true instead of one "loading — or none" line.
            dimText(!s_omp.available ? "Catalog unavailable." :
                    !ui::bridgeModelCatalog().empty() ? "No current models for this provider." :
                    ui::modelPickerDeferred() ? "Models appear when the current run finishes." :
                    ui::modelPickerLoading() ? "Loading models\xe2\x80\xa6" :
                    ui::modelPickerError() ? "Models could not be loaded. Use Retry model discovery "
                                             "in the model picker." :
                    "No models available for this provider.");
        }
        // Wide panes lay the checkbox rows out in two columns — a 26-model catalog stops
        // being one long single-file list. Column flow is index-based: a float fit-test on
        // GetItemRectMax() sits exactly on the cell boundary and never fires.
        const float listAvailW = ImGui::GetContentRegionAvail().x;
        const float modelGap = 20.0f * sc;
        const int modelCols = (count > 8 && listAvailW >= 640.0f * sc) ? 2 : 1;
        const float modelCellW =
            (listAvailW - modelGap * (float)(modelCols - 1)) / (float)modelCols;

        // Same checkbox+name+sublabel row for both the active list and the deprecated group.
        // The NAME is the primary content: it claims up to half the cell first; the sublabel
        // right-aligns into what's left. Both ellipsize instead of clipping.
        // NOTE: ImGui items wrap the cursor to the next line start, so every horizontal
        // position here is derived from x0 + CalcTextSize — never GetCursorPosX() after an
        // item (that reads the NEXT line's start, which silently corrupts cell math).
        auto drawModelRow = [&](const ui::BridgeModelMeta& m) {
            const float x0 = ImGui::GetCursorPosX();
            const std::string identity = m.provider + "/" + m.id;
            bool hidden = st.settings.hiddenModels.count(identity) ||
                (st.settings.hiddenModels.count(m.id) &&
                 !st.settings.shownLegacyHiddenModels.count(identity));
            bool shown = !hidden;
            if (ui::kit::checkbox("show", "", &shown)) {
                if (shown) {
                    st.settings.hiddenModels.erase(identity);
                    st.settings.shownLegacyHiddenModels.insert(identity);
                } else {
                    st.settings.hiddenModels.insert(identity);
                    st.settings.shownLegacyHiddenModels.erase(identity);
                }
                saveSettings(st.settings);
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Show/hide in the copilot model picker.");
            ImGui::SameLine(0, 6.0f * sc);
            const float nameX = ImGui::GetCursorPosX();   // on-line: valid
            const std::string nm =
                ellipsizeToFit(m.name, (x0 + modelCellW - nameX) * 0.5f);
            ImGui::TextUnformatted(nm.c_str());
            const float nameEnd = nameX + ImGui::CalcTextSize(nm.c_str()).x;
            const std::string sub = ui::bridgeModelSublabel(m);
            if (!sub.empty()) {
                const std::string subFit =
                    ellipsizeToFit(sub, x0 + modelCellW - nameEnd - 8.0f * sc);
                if (!subFit.empty()) {
                    const float subW = ImGui::CalcTextSize(subFit.c_str()).x;
                    const float rx = x0 + modelCellW - subW;
                    if (rx > nameEnd + 4.0f * sc) ImGui::SameLine(rx, 0);
                    else ImGui::SameLine(0, 6.0f * sc);
                    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                    ImGui::TextUnformatted(subFit.c_str());
                    ImGui::PopStyleColor();
                    const std::string detail = ui::bridgeModelCostDetail(m);
                    if (!detail.empty() && ImGui::IsItemHovered())
                        ImGui::SetTooltip("%s", detail.c_str());
                }
            }
            // Anchor the line's extent at the cell boundary so the index-based SameLine in
            // flowModelCell starts the next cell exactly at x0 + cellW + gap even when this
            // row's content ended early (no sublabel / short name).
            ImGui::SameLine(x0 + modelCellW, 0);
            ImGui::Dummy(ImVec2(0.0f, 1.0f));
        };
        int modelCellIdx = 0;
        auto flowModelCell = [&]() {
            if (modelCellIdx % modelCols != 0) ImGui::SameLine(0, modelGap);
            ++modelCellIdx;
        };

        // Catalog is already provider-sorted newest-first; render active models inline and
        // stash deprecated ones for the collapsed group below (search applies to both).
        std::vector<const ui::BridgeModelMeta*> deprecatedMatches;
        int matchCount = 0;
        for (const auto& m : ui::bridgeModelCatalog()) {
            if (m.provider != prov.id || ui::isLegacyGenerationModel(m.id)) continue;
            if (!modelMatches(m, modelNeedle)) continue;
            ++matchCount;
            if (m.deprecated) { deprecatedMatches.push_back(&m); continue; }
            ImGui::PushID(m.id.c_str());
            flowModelCell();
            drawModelRow(m);
            ImGui::PopID();
        }
        if (!deprecatedMatches.empty()) {
            char depTitle[48];
            std::snprintf(depTitle, sizeof(depTitle), "Older / deprecated (%d)",
                          (int)deprecatedMatches.size());
            if (ImGui::CollapsingHeader(depTitle)) {
                modelCellIdx = 0;
                for (const auto* mp : deprecatedMatches) {
                    ImGui::PushID(mp->id.c_str());
                    flowModelCell();
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

// Legacy credentials move only on explicit request, one acknowledged provider at a time.
// Values stay private to the request and are never placed in descriptors or status text.
struct LegacyKeyMove {
    std::string provider;
    std::string field;
    std::string value;
    std::string topField;
};
static std::vector<LegacyKeyMove> s_legacyKeyMoves;
static size_t s_legacyKeyIndex = 0;
static bool s_legacyKeyBusy = false;
static std::string s_legacyKeyStatus;

static std::vector<LegacyKeyMove> legacyKeyMoves() {
    static const std::pair<const char*, const char*> mappings[] = {
        {"anthropic", "anthropic"}, {"openai", "openai"}, {"gemini", "google"},
        {"xai", "xai"}, {"moonshot", "moonshot"}};
    std::vector<LegacyKeyMove> result;
    const auto config = readConfigJson();
    if (!config.is_object()) return result;
    const auto keys = config.value("keys", nlohmann::json::object());
    if (keys.is_object()) {
        for (const auto& mapping : mappings) {
            const auto it = keys.find(mapping.first);
            if (it != keys.end() && it->is_string() && !it->get_ref<const std::string&>().empty())
                result.push_back({mapping.second, mapping.first, it->get<std::string>(), ""});
        }
    }
    const std::string topField = config.contains("legacy_api_key") ? "legacy_api_key" : "api_key";
    const auto top = config.find(topField);
    const std::string oldProvider = config.value("legacy_api_key_provider",
                                                config.value("provider", std::string()));
    const auto endpoint = config.value("openai", nlohmann::json::object());
    const bool localTop = oldProvider == "openai" &&
        !config.contains("legacy_api_key_provider") && endpoint.is_object() &&
        ui::baseUrlIsLoopback(endpoint.value("base_url", std::string()).c_str());
    if (!localTop && top != config.end() && top->is_string() && !top->get_ref<const std::string&>().empty()) {
        for (const auto& mapping : mappings) {
            if (oldProvider != mapping.first) continue;
            const std::string value = top->get<std::string>();
            auto existing = std::find_if(result.begin(), result.end(), [&](const LegacyKeyMove& row) {
                return row.provider == mapping.second;
            });
            if (existing == result.end()) result.push_back({mapping.second, "", value, topField});
            else if (existing->value == value) existing->topField = topField;
            else result.erase(existing); // conflicting legacy keys require manual resolution
            break;
        }
    }
    return result;
}

static void moveNextLegacyKey(AppState& st) {
    if (s_legacyKeyIndex >= s_legacyKeyMoves.size()) {
        s_legacyKeyBusy = false;
        s_legacyKeyMoves.clear();
        return;
    }
    const LegacyKeyMove entry = s_legacyKeyMoves[s_legacyKeyIndex];
    st.rpc.call(ui::kOmpSetKeyMethod,
                nlohmann::json{{"providerId", entry.provider}, {"apiKey", entry.value}},
                [&st, entry](const core::RpcResult& reply) {
        bool moved = reply.ok && ui::ompKeySaveAccepted(reply.result, entry.provider);
        if (moved) {
            moved = updateConfigJson([&](nlohmann::json& config) {
                if (!entry.field.empty() && config.contains("keys") && config["keys"].is_object() &&
                    config["keys"].value(entry.field, std::string()) == entry.value)
                    config["keys"].erase(entry.field);
                if (!entry.topField.empty() && config.value(entry.topField, std::string()) == entry.value) {
                    config.erase(entry.topField);
                    config.erase("legacy_api_key_provider");
                }
            });
            if (moved) moved = flushConfigWrites();
            if (moved) {
                auto clearMatching = [&](char* key) {
                    if (entry.value == key) key[0] = 0;
                };
                if (entry.provider == "anthropic") clearMatching(st.settings.anthropicKey);
                else if (entry.provider == "openai") clearMatching(st.settings.openaiKey);
                else if (entry.provider == "google") clearMatching(st.settings.geminiKey);
                else if (entry.provider == "xai") clearMatching(st.settings.xaiKey);
                else if (entry.provider == "moonshot") clearMatching(st.settings.moonshotKey);
                if (entry.topField == "api_key" && st.settings.provider != ui::PROV_LOCAL &&
                    st.settings.provider != ui::PROV_LLAMACPP) clearMatching(st.settings.apiKey);
            }
        }
        if (!s_legacyKeyStatus.empty()) s_legacyKeyStatus += "\n";
        s_legacyKeyStatus += entry.provider + (moved ? ": key moved." :
                                              ": could not move key; it remains saved for retry.");
        s_legacyKeyMoves[s_legacyKeyIndex].value.clear();
        ++s_legacyKeyIndex;
        moveNextLegacyKey(st);
    }, 20000);
}

static bool hasLegacyKeys() {
    const auto config = readConfigJson();
    if (!config.is_object()) return false;
    const auto keys = config.value("keys", nlohmann::json::object());
    if (keys.is_object())
        for (auto it = keys.begin(); it != keys.end(); ++it)
            if (it.value().is_string() && !it.value().get_ref<const std::string&>().empty())
                return true;
    if (config.contains("legacy_api_key") && config["legacy_api_key"].is_string() &&
        !config["legacy_api_key"].get_ref<const std::string&>().empty()) return true;
    const auto top = config.find("api_key");
    if (top == config.end() || !top->is_string() || top->get_ref<const std::string&>().empty())
        return false;
    const auto endpoint = config.value("openai", nlohmann::json::object());
    return config.contains("legacy_api_key_provider") ||
           config.value("provider", std::string()) != "openai" || !endpoint.is_object() ||
           !ui::baseUrlIsLoopback(endpoint.value("base_url", std::string()));
}

static void drawLegacyKeyMigration(AppState& st) {
    const auto candidates = s_legacyKeyBusy ? std::vector<LegacyKeyMove>{} : legacyKeyMoves();
    if (!s_legacyKeyBusy && !hasLegacyKeys() && s_legacyKeyStatus.empty()) return;
    const bool disabled = s_legacyKeyBusy || candidates.empty();
    if (disabled) ImGui::BeginDisabled();
    if (ImGui::Button("Move saved API keys to OMP") && !candidates.empty()) {
        s_legacyKeyMoves = candidates;
        s_legacyKeyIndex = 0;
        s_legacyKeyBusy = true;
        s_legacyKeyStatus.clear();
        moveNextLegacyKey(st);
    }
    if (disabled) ImGui::EndDisabled();
    if (!s_legacyKeyBusy && candidates.empty() && hasLegacyKeys())
        dimText("Some saved keys need manual account setup; ambiguous providers are not moved.");
    if (!s_legacyKeyStatus.empty()) dimText(s_legacyKeyStatus.c_str());
}

// Two-pane provider manager: left = provider list with sign-in state, right = detail
// (Sign-in / Usage / Models). Footer = per-session cost strip. Single-column fallback under
// 640*sc: the list collapses to a combo (design §3.1).
static void drawOmpProviderManager(AppState& st) {
    const float sc = ui::uiScale;
    const auto& pal = ui::palette;
    if (const char* why = ui::webGapReason("account_signin")) {
        // The browser build has no provider credential path: the polls below drive omp_*
        // auth RPCs against the shared engine's own credential store.
        settingsGlassSectionBegin("##web_providers", "Providers");
        dimText(why);
        settingsGlassSectionEnd();
        return;
    }
    drawLegacyKeyMigration(st);
    enterOmpProviderView();
    pollOmpProviderList(st, false);
    ui::refreshBridgeModelCatalog(st, false);

    if (s_omp.oauth.active && s_omp.oauth.provider != s_omp.selectedId) {
        std::string flowLabel = s_omp.oauth.provider;
        for (const auto& row : s_omp.rows)
            if (row.id == s_omp.oauth.provider) {
                flowLabel = row.label;
                break;
            }
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::Text("Sign-in for %s is still active.", flowLabel.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine(0, 8.0f * sc);
        if (ImGui::SmallButton("Show sign-in"))
            selectOmpProvider(s_omp.oauth.provider);
        ImGui::SameLine(0, 6.0f * sc);
        if (ImGui::SmallButton("Cancel sign-in")) cancelOmpOAuth(st);
    }

    if (!s_omp.available) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("Provider sign-in is unavailable. Reconnect the runtime to retry.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_omp.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("%s", s_omp.lastError.c_str());
        ImGui::PopStyleColor();
        // A real failure (not a wait for a running turn) gets an explicit way to retry now.
        if (s_omp.listFailed && !s_omp.listInFlight && ImGui::SmallButton("Retry##prov_list"))
            refreshOmpProvider(st);
    }
    if (!s_omp.listPartial.empty()) {
        // A successful read that lost some entries: the list below is still live, so this is a
        // note rather than the panel-wide error above.
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("%s", s_omp.listPartial.c_str());
        ImGui::PopStyleColor();
    }

    const float footerH = ImGui::GetFrameHeightWithSpacing() + 4.0f * sc;
    const float paneH = -footerH;
    const float availW = ImGui::GetContentRegionAvail().x;
    const bool narrow = availW < 640.0f * sc;

    static char s_provSearchBuf[64] = "";
    {
        // Search + logos toggle share one row — they used to be two full-width rows. The
        // toggle's real kit width plus one item gap of right margin is reserved up front, so its
        // label never runs into the pane's right edge / scrollbar lane.
        const float gap = 8.0f * sc;
        const float logosW = settingsCheckboxWidth("Show provider logos");
        ImGui::SetNextItemWidth(
            ImMax(140.0f * sc, availW - logosW - gap - ImGui::GetStyle().ItemSpacing.x));
        ImGui::InputTextWithHint("##prov_search", "Search providers\xe2\x80\xa6", s_provSearchBuf,
                                 sizeof(s_provSearchBuf));
        ImGui::SameLine(0, gap);
        bool logosOn = ui::providerLogosEnabled();
        if (ui::kit::checkbox("prov_logos", "Show provider logos", &logosOn,
                              "Fetch each provider's real brand icon (cached locally).\n"
                              "Off = colored letter badges, no network."))
            ui::setProviderLogosEnabled(logosOn);
    }
    const std::string provNeedle = toLowerAscii(s_provSearchBuf);
    std::vector<const OmpProviderRow*> filteredRows;
    filteredRows.reserve(s_omp.rows.size());
    for (const auto& r : s_omp.rows)
        if (provMatches(r, provNeedle)) filteredRows.push_back(&r);

    // List pane width follows the widest visible label/status line (badge + gutters match the
    // row painter below), clamped so it neither truncates nor hogs the detail pane.
    float listW = 220.0f * sc;
    if (!narrow) {
        const float fh = ImGui::GetFontSize();
        float textW = 0.0f;
        for (const auto* rp : filteredRows) {
            const OmpProviderRow& r = *rp;
            const float statusW = fh * 0.56f + 5.0f * sc
                                  + ImGui::CalcTextSize(ompStateLabel(r)).x;
            textW = ImMax(textW, ImMax(ImGui::CalcTextSize(r.label.c_str()).x, statusW));
        }
        // Row padding (kit Compact density) + badge + gap + text + the list child's padding.
        const float need = ui::metrics::cpChatCardPadX() * 2.0f + fh * 1.4f + 8.0f * sc +
                           textW + ui::metrics::px(10.0f) * 2.0f;
        listW = ImMin(ImMax(need, 180.0f * sc), ImMin(320.0f * sc, availW * 0.45f));
    }

    if (narrow) {
        // Collapsed: provider pick as a combo, detail below.
        std::string preview = s_omp.selectedId;
        for (const auto& r : s_omp.rows)
            if (r.id == s_omp.selectedId) { preview = r.label; break; }
        ImGui::SetNextItemWidth(-1);
        const float badgeSide = ImGui::GetFontSize() * 1.1f;
        float widestProviderRow = 0.0f;
        for (const auto* rp : filteredRows) {
            const float labelWidth = ImGui::CalcTextSize(rp->label.c_str()).x;
            const float statusWidth = ImGui::CalcTextSize(ompStateLabel(*rp)).x;
            widestProviderRow = ImMax(widestProviderRow,
                badgeSide + 10.0f * sc + labelWidth + statusWidth);
        }
        if (settingsBeginPopupRowCombo("##omp_prov_pick", preview.c_str(),
                                       widestProviderRow)) {
            for (int i = 0; i < (int)filteredRows.size(); ++i) {
                const OmpProviderRow& r = *filteredRows[i];
                ImGui::PushID(r.id.c_str());
                ImDrawFlags corners = ImDrawFlags_RoundCornersNone;
                if (i == 0) corners |= ImDrawFlags_RoundCornersTop;
                if (i == (int)filteredRows.size() - 1)
                    corners |= ImDrawFlags_RoundCornersBottom;
                const ImVec2 rowPos = ImGui::GetCursorScreenPos();
                const float rowHeight = ImMax(ImGui::GetFrameHeight(), badgeSide);
                if (settingsPopupRowSelectable("##provider_row",
                                               r.id == s_omp.selectedId,
                                               rowHeight, corners))
                    selectOmpProvider(r.id);

                ImDrawList* dl = ImGui::GetWindowDrawList();
                const float textY =
                    rowPos.y + (rowHeight - ImGui::GetTextLineHeight()) * 0.5f;
                const ImVec2 badgePos(
                    rowPos.x + ImGui::GetStyle().FramePadding.x,
                    rowPos.y + (rowHeight - badgeSide) * 0.5f);
                ui::drawProviderLogo(dl, badgePos, badgeSide, r.id);
                const float labelX = badgePos.x + badgeSide + 4.0f * sc;
                dl->AddText(ImVec2(labelX, textY), ImGui::GetColorU32(ImGuiCol_Text),
                            r.label.c_str());
                const float* c = ompStateColor(r.state);
                dl->AddText(
                    ImVec2(labelX + ImGui::CalcTextSize(r.label.c_str()).x + 6.0f * sc, textY),
                    ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], 1.0f)),
                    ompStateLabel(r));
                ImGui::PopID();
            }
            if (filteredRows.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
                ImGui::TextWrapped("%s", ompEmptyListMessage());
                ImGui::PopStyleColor();
            }
            settingsEndPopupRowCombo();
        }
    } else {
        // Both halves of the split view share one inner padding — they used to run 8/6 on the
        // left and 0/0 on the right, which made the two panes read as different widgets.
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
        ImGui::BeginChild("##omp_prov_list", ImVec2(listW, paneH),
                          ImGuiChildFlags_Borders);
        for (const auto* rp : filteredRows) {
            const OmpProviderRow& r = *rp;
            ImGui::PushID(r.id.c_str());
            const bool sel = (r.id == s_omp.selectedId);
            // Same kit row (rounded glass pill, hover wash, selection glow) as the AI rail and
            // every other selectable list in the app; the badge and two text lines are drawn
            // into its padded body.
            const float fh = ImGui::GetFontSize();
            ui::kit::pushDensity(ui::kit::Density::Compact);
            const float bodyH = fh * 2.0f + 2.0f * sc;
            const bool clicked = ui::kit::rowBegin("##row", bodyH + ui::kit::padY() * 2.0f, sel);
            const ImVec2 mn = ImGui::GetItemRectMin();
            const ImVec2 mx = ImGui::GetItemRectMax();
            const ImVec2 body = ImGui::GetCursorScreenPos();
            const float bodyRight = mx.x - (body.x - mn.x);
            ui::kit::rowEnd();
            ui::kit::popDensity();
            if (clicked) selectOmpProvider(r.id);
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float badgeSide = fh * 1.4f;
            const float badgeGap = 8.0f * sc;
            const float badgeX = body.x;
            const float badgeY = mn.y + ((mx.y - mn.y) - badgeSide) * 0.5f;
            // The two-line text block (name + status) fills the padded body, whose centre is
            // the row centre, so badge, name and status read as one vertically-centred unit.
            const float textTop = body.y;
            ui::drawProviderLogo(dl, ImVec2(badgeX, badgeY), badgeSide, r.id);
            const float textX = badgeX + badgeSide + badgeGap;
            const float textMaxW = ImMax(bodyRight - textX, ui::metrics::stroke(1.0f));
            const std::string labelFit = providerListLabel(r.label, textMaxW);
            if (labelFit != r.label && ImGui::IsWindowHovered() &&
                ImGui::IsMouseHoveringRect(mn, mx))
                ui::kit::tooltip(r.label.c_str());
            dl->AddText(ImVec2(textX, textTop),
                        ui::Palette::toU32(sel ? pal.text : pal.textDim), labelFit.c_str());
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
            ImGui::TextWrapped("%s", ompEmptyListMessage());
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

    // Footer: per-session cost strip (omp_session_stats; hides itself when unavailable).
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
enum class RoleMode { Default, Auto, Fixed, Tier, LegacyWorker, Size };
static constexpr const char* kRoleModeDefaultLabel = "Use chat model";
static constexpr const char* kRoleModeAutoLabel = "Previous ranked Auto needs a new assignment";

// Wire entry → mode: explicit mode wins, else a provider/model pin, else Auto.
static RoleMode roleModeFromWire(const std::string& mode, const std::string& provider,
                                 const std::string& model) {
    if (mode == "tier") return RoleMode::Tier;
    if (mode == "size") return RoleMode::Size;
    if (mode == "default") return RoleMode::Default;
    if (mode == "auto") return RoleMode::Auto;
    if (!provider.empty() || !model.empty()) return RoleMode::Fixed;
    return RoleMode::Default;
}

struct RoleAssign {
    RoleMode mode = RoleMode::Default;
    std::string provider;
    std::string model;
    // Per-role reasoning effort: "auto" | "low" | "medium" | "high" (default auto).
    std::string thinking = "auto";
    // Size-mode override: the tier it dispatches through ("small" | "medium" | "large").
    std::string size;
};
struct ModelRolesUi {
    RoleAssign chat;
    std::set<std::string> activeOverrides;
    bool legacyMissionWorkerFallback = false;
    // Per-role overrides apply only while this is on; off = every role dispatches via its tier.
    bool advancedOverrides = false;
    std::set<std::string> legacyMissionFallbackExclusions;
    std::map<std::string, RoleAssign> roles;
    std::map<std::string, RoleAssign> sizes;
    std::map<std::string, std::string> roleSizes;
    std::string assignmentMode = "simple";
    bool rolesLoaded = false;
    bool rolesRpcMissing = false;
    bool rolesPollInFlight = false;
    bool rolesPollSuperseded = false;
    // The last ai_roles_get/ai_roles_set answer was "a running agent turn holds the service
    // lane": a wait. The loaded assignments stay; nothing was read or saved meanwhile.
    bool rolesDeferred = false;
    double lastRolesPoll = -1e9;
    std::string lastError;
    bool rolesSaveInFlight = false;
    nlohmann::json pendingPatch;
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
    // Letter ink picked against the badge's own hue: every kRoleColors entry is a pastel above the
    // 0.5 luminance split, so white was the low-contrast choice on all fifteen.
    ImVec4 ink = ui::maxContrastTextOn(col);
    ink.w = 240.0f / 255.0f;
    dl->AddText(ImVec2(p.x + (sz - ts.x) * 0.5f, p.y + (sz - ts.y) * 0.5f),
                ImGui::ColorConvertFloat4ToU32(ink), glyph);
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
        s_bench.running = optionalJsonBool(r.result, "running", false);
        s_bench.stage = optionalJsonString(r.result, "stage");
        if (s_bench.stage.empty()) s_bench.stage = "idle";
        s_bench.lastError = optionalJsonString(r.result, "last_error");
        if (s_bench.lastError == "null") s_bench.lastError.clear();
        s_bench.lastResultPath = optionalJsonString(r.result, "last_result_path");
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


// Candidate provider IDs accepted by the worker's redaction policy. The worker also verifies
// the configured endpoint is loopback before saving; a generic HTTP probe cannot establish this.
static bool providerMayBeLoopback(const std::string& provider) {
    return provider == "llamacpp" || provider == "llama.cpp" || provider == "llama-cpp" ||
           provider == "lmstudio" || provider == "ollama" || provider == "vllm";
}

// Preserve the exact OMP provider ID, even when multiple providers share a display slot.
struct RolePickEntry {
    RoleMode mode = RoleMode::Fixed;
    std::string wire;
    std::string model;
    std::string label;
};

static bool rolePickMatches(const RolePickEntry& pick, const char* filter) {
    if (!filter[0]) return true;
    const char* end = filter + std::strlen(filter);
    const auto contains = [filter, end](const std::string& text) {
        return std::search(text.begin(), text.end(), filter, end, [](char a, char b) {
            return std::tolower(static_cast<unsigned char>(a)) ==
                   std::tolower(static_cast<unsigned char>(b));
        }) != text.end();
    };
    return contains(pick.label) || contains(pick.model);
}

// Cache the built rows, not a copy of the runtime catalog on every settings frame.
static const std::vector<RolePickEntry>& buildRolePickList(AppState& st, bool localOnly,
                                                            bool includeAuto = true) {
    ui::refreshBridgeModelCatalog(st, false);
    ui::refreshSignedInProviders(st, false);
    const auto& catalog = ui::bridgeModelCatalog();
    size_t signature = 0;
    auto mix = [&](size_t n) {
        signature ^= n + size_t(0x9e3779b9) + (signature << 6) + (signature >> 2);
    };
    auto text = [&](const std::string& s) { mix(std::hash<std::string>{}(s)); };
    for (const auto& m : catalog) {
        text(m.provider); text(m.id); text(m.name); mix(m.deprecated);
    }
    for (const auto& p : st.signedInBridgeProviders) text(p);
    for (const auto& p : st.settings.disabledProviders) text(p);
    for (const auto& m : st.settings.hiddenModels) text(m);
    for (const auto& m : st.settings.shownLegacyHiddenModels) text(m);
    mix(ui::modelPickerLoading()); mix(ui::modelPickerError()); mix(ui::modelPickerDeferred());
    struct Cache { size_t signature = 0; bool valid = false; std::vector<RolePickEntry> rows; };
    static Cache caches[2][2];
    Cache& cache = caches[localOnly ? 1 : 0][includeAuto ? 1 : 0];
    if (cache.valid && cache.signature == signature) return cache.rows;
    cache.signature = signature;
    cache.valid = true;
    auto& out = cache.rows;
    out.clear();
    if (!localOnly) {
        // A per-role override (includeAuto) picks a tier or a concrete model; following the chat
        // model is no override at all, so only the tier lists (chat, sizes) offer it.
        if (includeAuto) {
            out.push_back({RoleMode::Tier, "", "", "Use tier assignment"});
            out.push_back({RoleMode::Size, "", "small", "Small tier"});
            out.push_back({RoleMode::Size, "", "medium", "Medium tier"});
            out.push_back({RoleMode::Size, "", "large", "Large tier"});
        } else {
            out.push_back({RoleMode::Default, "", "", kRoleModeDefaultLabel});
        }
    }
    // A legacy HTTP probe shares its rows across provider slots and supplies no OMP identity.
    // Only the runtime catalog can provide a real provider/model pair for an online assignment.
    for (const auto& m : catalog) {
        if (!ui::bridgePickerModelUsable(st, m) ||
            (localOnly && !providerMayBeLoopback(m.provider))) continue;
        out.push_back({RoleMode::Fixed, m.provider, m.id, m.provider + " \xc2\xb7 " + m.name});
    }
    return out;
}

static void ensureDefaultRoles() {
    for (int i = 0; i < kRoleCount; ++i) {
        if (s_roles.roles.find(kRoleKeys[i]) == s_roles.roles.end()) {
            RoleAssign a;
            a.mode = RoleMode::Default;
            s_roles.roles[kRoleKeys[i]] = a;
        }
    }
}

static const char* kSizeKeys[] = {"small", "medium", "large"};

static RoleAssign roleAssignFromJson(const nlohmann::json& value, RoleMode fallback) {
    RoleAssign a;
    a.mode = fallback;
    if (value.is_string()) {
        a.mode = value.get<std::string>() == "default" ? RoleMode::Default : RoleMode::Auto;
    } else if (value.is_object()) {
        a.provider = optionalJsonString(value, "provider");
        a.model = optionalJsonString(value, "model");
        a.size = optionalJsonString(value, "size");
        const std::string mode = optionalJsonString(value, "mode");
        if (!mode.empty() || !a.provider.empty() || !a.model.empty())
            a.mode = roleModeFromWire(mode, a.provider, a.model);
        a.thinking = optionalJsonString(value, "thinking");
        if (a.thinking.empty()) a.thinking = "auto";
    }
    return a;
}

static void applyModelRolesReply(const nlohmann::json& result) {
    const auto chat = result.find("chat_assignment");
    s_roles.chat = chat == result.end() ? RoleAssign{} : roleAssignFromJson(*chat, RoleMode::Default);
    s_roles.activeOverrides.clear();
    const auto active = result.find("active_role_overrides");
    if (active != result.end() && active->is_array())
        for (const auto& role : *active)
            if (role.is_string()) s_roles.activeOverrides.insert(role.get<std::string>());
    s_roles.legacyMissionWorkerFallback =
        optionalJsonBool(result, "legacy_mission_worker_fallback", false);
    s_roles.legacyMissionFallbackExclusions.clear();
    const auto exclusions = result.find("legacy_mission_worker_fallback_exclusions");
    if (exclusions != result.end() && exclusions->is_array())
        for (const auto& role : *exclusions)
            if (role.is_string()) s_roles.legacyMissionFallbackExclusions.insert(role.get<std::string>());
    s_roles.assignmentMode = result.value("assignment_mode", std::string("simple"));
    s_roles.advancedOverrides = optionalJsonBool(result, "advanced_overrides", false);
    auto roles = optionalJsonObj(result, "roles");
    s_roles.roles.clear();
    for (const char* key : kRoleKeys) {
        auto it = roles.find(key);
        s_roles.roles[key] = it == roles.end() ? RoleAssign{} :
            roleAssignFromJson(*it, RoleMode::Default);
    }
    auto sizes = optionalJsonObj(result, "model_sizes");
    s_roles.sizes.clear();
    for (const char* key : kSizeKeys) {
        auto it = sizes.find(key);
        RoleAssign a;
        a.mode = RoleMode::Default;
        if (it != sizes.end()) a = roleAssignFromJson(*it, RoleMode::Default);
        s_roles.sizes[key] = std::move(a);
    }
    s_roles.roleSizes.clear();
    auto roleSizes = optionalJsonObj(result, "role_sizes");
    for (auto it = roleSizes.begin(); it != roleSizes.end(); ++it)
        if (it.value().is_string()) s_roles.roleSizes[it.key()] = it.value().get<std::string>();
}

static void pollModelRoles(AppState& st, bool force) {
    const double now = ImGui::GetTime();
    // A read that has not landed yet normally retries at once; while a running turn defers it
    // (rolesLoaded stays false), that would be one refused call per frame.
    if (!force && (s_roles.rolesLoaded || s_roles.rolesDeferred) &&
        (now - s_roles.lastRolesPoll) < 5.0)
        return;
    if (s_roles.rolesPollInFlight || s_roles.rolesSaveInFlight) return;
    s_roles.lastRolesPoll = now;
    s_roles.rolesPollInFlight = true;
    s_roles.rolesPollSuperseded = false;
    st.rpc.call("ai_roles_get", nlohmann::json::object(), [&st](const core::RpcResult& r) {
        s_roles.rolesPollInFlight = false;
        // A write started after this read; its snapshot must not replace the save
        // result (or the last confirmed state if the save fails).
        if (s_roles.rolesPollSuperseded) return;
        // A running agent turn holds hostd's agent lane (ai_roles_* are served from it) and
        // every other call on it is refused at once. That is neither an empty read nor a
        // missing service: its envelope carries no `ok`, and reading that as success replaced
        // the loaded assignments with defaults every 5 s for as long as the run lasted. Keep
        // what was confirmed and ask again at the normal cadence.
        s_roles.rolesDeferred = core::workerLaneBusy(r);
        if (s_roles.rolesDeferred) return;
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !optionalJsonBool(r.result, "ok", false)) {
            // The technical cause goes to the log; the panel says what the user can expect.
            st.addLog("[models] model assignments could not be read: " +
                      (!r.ok ? r.err : optionalJsonString(r.result, "reason")));
            s_roles.rolesRpcMissing = true;
            if (s_roles.pendingPatch.is_null())
                s_roles.lastError =
                    "Model assignments could not be loaded. It will try again automatically.";
            ensureDefaultRoles();
            s_roles.rolesLoaded = true;
            return;
        }
        s_roles.rolesRpcMissing = false;
        if (s_roles.pendingPatch.is_null()) s_roles.lastError.clear();
        applyModelRolesReply(r.result);
        s_roles.rolesLoaded = true;
    });
}

static nlohmann::json roleAssignToJson(const RoleAssign& a, bool thinking) {
    nlohmann::json entry = nlohmann::json::object();
    if (a.mode == RoleMode::Tier) entry["mode"] = "tier";
    else if (a.mode == RoleMode::Default) entry["mode"] = "default";
    else if (a.mode == RoleMode::Size) { entry["mode"] = "size"; entry["size"] = a.size; }
    else if (a.mode == RoleMode::Auto) entry["mode"] = "auto";
    else {
        entry["mode"] = "fixed";
        entry["provider"] = a.provider;
        entry["model"] = a.model;
    }
    if (thinking) entry["thinking"] = a.thinking.empty() ? "auto" : a.thinking;
    return entry;
}

static void sendPendingModelRoles(AppState& st) {
    if (s_roles.pendingPatch.is_null() || s_roles.rolesSaveInFlight) return;
    if (s_roles.rolesPollInFlight) s_roles.rolesPollSuperseded = true;
    s_roles.rolesSaveInFlight = true;
    s_roles.lastError.clear();
    st.rpc.call("ai_roles_set", s_roles.pendingPatch, [](const core::RpcResult& r) {
        s_roles.rolesSaveInFlight = false;
        if (core::workerLaneBusy(r)) {
            // The change was NOT saved: the draft stays, with Retry / Discard.
            s_roles.rolesDeferred = true;
            s_roles.lastError =
                "Saving model assignments is available when the current run finishes.";
            return;
        }
        if (!r.ok || !r.result.is_object() || r.result.contains("code") ||
            !optionalJsonBool(r.result, "ok", false)) {
            s_roles.lastError = !r.ok ? (r.err.empty() ? "Could not save model assignments." : r.err)
                : r.result.is_object() ? r.result.value("reason", std::string("Could not save model assignments."))
                                       : "Invalid model assignment response.";
            return;
        }
        if (!r.result.contains("assignment_mode") || !r.result.contains("model_sizes") ||
            !r.result.contains("roles")) {
            s_roles.lastError = "The worker did not confirm the saved model assignments.";
            return;
        }
        applyModelRolesReply(r.result);
        s_roles.pendingPatch = nullptr;
        s_roles.rolesRpcMissing = false;
        s_roles.lastError.clear();
        s_roles.lastRolesPoll = ImGui::GetTime();
    });
}

static void saveModelRoles(AppState& st, const nlohmann::json& params) {
    if (s_roles.rolesRpcMissing || !s_roles.rolesLoaded || !s_roles.pendingPatch.is_null() ||
        s_roles.rolesSaveInFlight) return;
    s_roles.pendingPatch = params;
    sendPendingModelRoles(st);
}

static RoleAssign modelRolesDraft(const char* field, const char* key, const RoleAssign& confirmed) {
    const auto& patch = s_roles.pendingPatch;
    if (!patch.is_object()) return confirmed;
    auto group = patch.find(field);
    if (group == patch.end() || !group->is_object()) return confirmed;
    auto entry = group->find(key);
    return entry == group->end() ? confirmed : roleAssignFromJson(*entry, confirmed.mode);
}

static void setRoleAssign(AppState& st, const char* roleKey, const RoleAssign& a) {
    saveModelRoles(st, {{"roles", {{roleKey, roleAssignToJson(a, true)}}}});
}

static void setSizeAssign(AppState& st, const char* sizeKey, const RoleAssign& a) {
    saveModelRoles(st, {{"model_sizes", {{sizeKey, roleAssignToJson(a, true)}}}});
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
        dimText("Idle \xe2\x80\x94 no legacy run in progress.");
    }
    (void)st; (void)sc;
}

// Legacy per-tool-family category table. The Benchmarks page draws it only when the worker
// reported categories (no empty/unavailable card), so this body assumes a non-empty list.
static void drawBenchCategories(AppState& st, float sc) {

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
}

static void drawBenchRunner(AppState& st, float sc, ui::leaderboard::Board& lb,
                            double& lbLast) {
    ImGui::Dummy(ImVec2(0, 6.0f * sc));
    ImGui::TextUnformatted("Legacy quick run");
    dimText("Spawns the retired gym CLI detached on the worker (diag / C1 smoke). On finish it "
            "rebuilds benchmarks/leaderboard/leaderboard.json and refreshes the table above.");

    static const char* kSources[] = {
        "Current copilot model",
        "xAI grok-4.5",
        "Local models (local-24k)",
    };
    static const char* kModes[] = {
        "Quick diag",
        "C1 smoke",
    };
    {
        ui::kit::FieldGroup group("bench_run", "Model");
        ui::kit::fieldEnum("bench_src", "Model", &s_bench.source, kSources, 3);
        ui::kit::fieldEnum("bench_mode", "Run", &s_bench.mode, kModes, 2);
    }

    if (benchSourceIsPaid(st.settings, s_bench.source)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("Uses your API credits / subscription for the selected model.");
        ImGui::PopStyleColor();
    }

    const bool canRun = !s_bench.running && st.workerStarted;
    if (ui::kit::primaryButton("Run benchmark", canRun, nullptr, ImVec2(160.0f * sc, 0))) {
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

static void settingsAuditLastText(const char* text) {
    ImGuiContext& g = *GImGui;
    IMGUI_TEST_ENGINE_ITEM_INFO(0, text, g.LastItemData.StatusFlags);
}

static void settingsAuditedDimText(const char* text) {
    dimText(text);
    settingsAuditLastText(text);
}

// Keep search and status outside the scrolling list, including when only one row is available.
// The list is as tall as its rows (at least 3, at most kPickerMaxRows) and scrolls inside the
// popover beyond that, so a long catalog never turns the picker into a page-long drop-down.
static constexpr int kPickerMaxRows = 10;
static void settingsBeginModelResults(float contentHeight) {
    const float rowH = ImGui::GetFrameHeight();
    const ImGuiViewport* vp = ImGui::GetWindowViewport();
    // Reserve room for search, wrapped status, and the retry/navigation buttons even at HiDPI.
    const int maxRows = std::max(1, std::min(kPickerMaxRows, static_cast<int>(
        (vp->WorkSize.y - 2.0f * ImGui::GetStyle().DisplaySafeAreaPadding.y) / rowH) - 6));
    const float height = ImClamp(contentHeight, static_cast<float>(std::min(3, maxRows)) * rowH,
                                 static_cast<float>(maxRows) * rowH);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##model_results", ImVec2(0.0f, height),
                      ImGuiChildFlags_NavFlattened);
}

static void settingsEndModelResults() {
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

static void settingsModelPickerStatus(AppState& st, const std::vector<RolePickEntry>& picks,
                                      int visibleCount, bool localOnly) {
    const bool loading = ui::modelPickerLoading();
    const bool error = ui::modelPickerError();
    const bool deferred = ui::modelPickerDeferred();
    if (loading) settingsAuditedDimText("Loading provider models...");
    if (error) {
        settingsAuditedDimText("Some provider information could not be refreshed. "
                               "Previously confirmed models may still be shown.");
        if (ImGui::SmallButton("Retry model discovery")) {
            ui::refreshBridgeModelCatalog(st, true);
            ui::refreshSignedInProviders(st, true);
        }
    }
    const bool haveModel = std::any_of(picks.begin(), picks.end(),
        [](const RolePickEntry& pick) { return pick.mode == RoleMode::Fixed; });
    // A running turn keeps the live read waiting: not an error, and not "you have no models".
    if (deferred)
        settingsAuditedDimText(haveModel
            ? "Showing models from your last session. They refresh when the current run finishes."
            : "Models appear when the current run finishes.");
    else if (!loading && !error && !haveModel)
        settingsAuditedDimText(localOnly ? "No available local models. Configure a local provider."
                                        : "No available models. Sign in or configure a provider.");
    else if (visibleCount == 0)
        settingsAuditedDimText("No models match your filter.");
    if ((!haveModel || error) && ImGui::SmallButton("Manage providers")) {
        ImGui::CloseCurrentPopup();
        st.openSettings = true;
        st.openSettingsTab = SettingsProviders;
    }
}

// The one model picker behind every model choice on the AI settings pages (chat model, size
// tiers, per-role overrides, Copilot Quick settings): a kit-faced control opening a compact
// popover — filter on top, a bounded scrolling list (tier / special choices first, then models
// grouped under small provider headers) with the current choice highlighted, status and
// recovery actions underneath. `width` < 0 fills the line.
static void drawAssignmentPicker(AppState& st, const char* key, const RoleAssign& a,
                                 const std::vector<RolePickEntry>& picks, bool isRedaction,
                                 bool isSize, bool ro, bool isChat = false, float width = -1.0f) {
    // Preserve unavailable and historical selections without selecting another row.
    int sel = -1;
    for (int pi = 0; pi < (int)picks.size(); ++pi) {
        if (a.mode == RoleMode::Fixed) {
            if (picks[pi].mode == RoleMode::Fixed && picks[pi].model == a.model &&
                picks[pi].wire == a.provider) {
                sel = pi;
                break;
            }
        } else if (picks[pi].mode == a.mode &&
                   (a.mode != RoleMode::Size || picks[pi].model == a.size)) {
            sel = pi;
        }
    }

    // Preview label. An unmatched pin still shows its raw wire value.
    std::string preview;
    if (a.mode == RoleMode::LegacyWorker) {
        preview = "Mission: legacy worker fallback";
    } else if (a.mode == RoleMode::Tier) {
        preview = "Use tier assignment";
    } else if (a.mode == RoleMode::Size) {
        preview = sel >= 0 ? picks[sel].label : "Unknown tier";
    } else if (a.mode == RoleMode::Default) {
        preview = isRedaction ? "Choose a local model" :
                  isChat ? "Runtime default" : kRoleModeDefaultLabel;
    } else if (a.mode == RoleMode::Auto) {
        preview = kRoleModeAutoLabel;
    } else if (sel >= 0) {
        preview = picks[sel].label;
    } else {
        // A pin the policy hides (Claude < 5.1 / GPT < 6) still shows as the current
        // selection; it is just no longer offered in the list.
        preview = a.provider + " \xc2\xb7 " + a.model +
                  (ui::isLegacyGenerationModel(a.model) ? " (older model, not offered)"
                                                        : " (unavailable)");
    }

    if (ro) ImGui::BeginDisabled();
    ImGui::SetNextItemWidth(width);
    float widestPick = 0.0f;
    for (const auto& pick : picks)
        widestPick = ImMax(widestPick, ImGui::CalcTextSize(pick.label.c_str()).x);
    if (settingsBeginPopupRowCombo("##role_pick", preview.c_str(), widestPick,
                                   ImGuiComboFlags_HeightLarge, 280.0f * ui::uiScale)) {
        // Type-to-filter — list is already filtered to keyed/signed-in providers.
        static char s_roleFilt[64] = "";
        if (ImGui::IsWindowAppearing()) {
            s_roleFilt[0] = 0;
            ImGui::SetKeyboardFocusHere();
        }
        ImGui::SetNextItemWidth(-1);
        ImGui::InputTextWithHint("##role_filt", "Type to filter...",
                                 s_roleFilt, sizeof(s_roleFilt));

        // Visible rows: specials first (no header), then concrete models grouped by provider in
        // the catalog's own order.
        struct Group { std::string wire; std::vector<int> rows; };
        std::vector<int> specials;
        std::vector<Group> groups;
        for (int pi = 0; pi < (int)picks.size(); ++pi) {
            if (!rolePickMatches(picks[pi], s_roleFilt)) continue;
            if (picks[pi].mode != RoleMode::Fixed) {
                specials.push_back(pi);
                continue;
            }
            auto group = std::find_if(groups.begin(), groups.end(),
                [&](const Group& g) { return g.wire == picks[pi].wire; });
            if (group == groups.end()) {
                groups.push_back({picks[pi].wire, {}});
                group = groups.end() - 1;
            }
            group->rows.push_back(pi);
        }
        // Providers read alphabetically so a group is findable at a glance; models keep the
        // catalog's order inside their group.
        std::stable_sort(groups.begin(), groups.end(),
                         [](const Group& l, const Group& r) { return l.wire < r.wire; });
        int visibleCount = static_cast<int>(specials.size());
        for (const auto& group : groups) visibleCount += static_cast<int>(group.rows.size());
        const float rowH = ImGui::GetFrameHeight();
        const float headH = ImGui::GetTextLineHeight() + ui::metrics::px(8.0f);
        const int firstRow = !specials.empty() ? specials.front()
                           : !groups.empty() ? groups.front().rows.front() : -1;
        const int lastRow = !groups.empty() ? groups.back().rows.back()
                          : !specials.empty() ? specials.back() : -1;

        auto drawRow = [&](int pi, bool grouped) {
            ImGui::PushID(pi);
            const bool selected = (pi == sel);
            ImDrawFlags corners = ImDrawFlags_RoundCornersNone;
            if (pi == firstRow) corners |= ImDrawFlags_RoundCornersTop;
            if (pi == lastRow) corners |= ImDrawFlags_RoundCornersBottom;
            const std::string fullLabel = isChat && picks[pi].mode == RoleMode::Default
                ? std::string("Runtime default") : picks[pi].label;
            // Under a provider header the row shows the model name only; the observable label
            // stays the full "provider · model".
            std::string shown = fullLabel;
            if (grouped && fullLabel.compare(0, picks[pi].wire.size(), picks[pi].wire) == 0) {
                const size_t sep = fullLabel.find(" \xc2\xb7 ");
                if (sep != std::string::npos) shown = fullLabel.substr(sep + 4);
            }
            const bool clicked = settingsPopupRowSelectable(shown.c_str(), selected, 0.0f, corners);
            // Open on the current choice even when it sits deep in a long catalog.
            if (selected && ImGui::IsWindowAppearing()) ImGui::SetScrollHereY(0.5f);
            {
                ImGuiContext& g = *GImGui;
                IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), fullLabel.c_str(),
                    g.LastItemData.StatusFlags | ImGuiItemStatusFlags_Checkable |
                    (selected ? ImGuiItemStatusFlags_Checked : 0));
            }
            if (clicked) {
                RoleAssign next = a;  // preserve thinking
                next.mode = picks[pi].mode;
                next.provider.clear();
                next.model.clear();
                next.size.clear();
                if (picks[pi].mode == RoleMode::Fixed) {
                    next.provider = picks[pi].wire;
                    next.model = picks[pi].model;
                } else if (picks[pi].mode == RoleMode::Size) {
                    next.size = picks[pi].model;
                }
                if (isRedaction && next.mode == RoleMode::Fixed &&
                           !providerMayBeLoopback(picks[pi].wire)) {
                    s_roles.lastError =
                        "Redaction role refuses non-loopback providers.";
                } else {
                    s_roles.lastError.clear();
                    if (isChat) saveModelRoles(st, {{"chat_assignment", roleAssignToJson(next, false)}});
                    else if (isSize) setSizeAssign(st, key, next);
                    else setRoleAssign(st, key, next);
                }
                // Selectable's implicit popup close only applies to popup windows, not
                // the scrolling child that owns these rows.
                ImGui::CloseCurrentPopup();
            }
            ImGui::PopID();
        };

        settingsBeginModelResults(static_cast<float>(visibleCount) * rowH +
                                  static_cast<float>(groups.size()) * headH);
        for (int pi : specials) drawRow(pi, false);
        for (const auto& group : groups) {
            const ImVec2 p = ImGui::GetCursorScreenPos();
            ImGui::Dummy(ImVec2(ImGui::GetContentRegionAvail().x, headH));
            ImGui::GetWindowDrawList()->AddText(
                ImVec2(p.x + ImGui::GetStyle().FramePadding.x,
                       p.y + headH - ImGui::GetTextLineHeight() - ui::metrics::px(2.0f)),
                ui::Palette::toU32(ui::palette.textDim), group.wire.c_str());
            for (int pi : group.rows) drawRow(pi, true);
        }
        settingsEndModelResults();
        settingsModelPickerStatus(st, picks, visibleCount, isRedaction);
        settingsEndPopupRowCombo();
    }
    if (ro) ImGui::EndDisabled();
    // A split row (explicit width) has no room for the wrapped note; the combo's tooltip carries
    // the full text there.
    if (width < 0.0f && ((a.mode == RoleMode::Fixed && sel < 0) || a.mode == RoleMode::Auto ||
        ImGui::CalcTextSize(preview.c_str()).x > ImGui::GetContentRegionAvail().x)) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextWrapped("%s", preview.c_str());
        settingsAuditLastText(preview.c_str());
        ImGui::PopStyleColor();
    }
}

// Select only levels reported for this exact model. Missing metadata is not an empty ladder.
// `autoLabel` names the unset choice ("Runtime default" per role, "Auto" per tier); `width` < 0
// fills the line.
static bool drawThinkingChoice(const char* id, const std::string& provider,
                               const std::string& model, std::string& effort,
                               const char* autoLabel = "Runtime default", float width = -1.0f) {
    const auto* meta = ui::bridgeModelMeta(provider, model);
    const bool known = meta && meta->reasoningEffortsKnown;
    const bool isDefault = effort.empty() || effort == "auto";
    const bool supported = known && std::find(meta->reasoningEfforts.begin(),
                                              meta->reasoningEfforts.end(), effort)
                                      != meta->reasoningEfforts.end();
    std::string preview = isDefault ? autoLabel : effort;
    if (!isDefault && !supported) preview += known ? " (unsupported)" : " (unknown)";
    bool changed = false;
    ImGui::SetNextItemWidth(width);
    if (settingsBeginPopupRowCombo(id, preview.c_str(), 140.0f * ui::uiScale,
                                   ImGuiComboFlags_HeightLarge)) {
        if (settingsPopupRowSelectable(autoLabel, isDefault)) {
            effort = "auto";
            changed = true;
        }
        if (isDefault) ImGui::SetItemDefaultFocus();
        if (known) {
            for (const auto& level : meta->reasoningEfforts) {
                const bool selected = effort == level;
                if (settingsPopupRowSelectable(level.c_str(), selected)) {
                    effort = level;
                    changed = true;
                }
                if (selected) ImGui::SetItemDefaultFocus();
            }
        } else {
            dimText("Thinking capabilities unavailable for this model.");
        }
        settingsEndPopupRowCombo();
    }
    if (!isDefault && !supported) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("%s", preview.c_str());
        settingsAuditLastText(preview.c_str());
        ImGui::PopStyleColor();
    }
    return changed;
}

// Read-only notice, last save error, and the unsaved/saving draft with its Retry/Discard actions.
// Shared by every surface that edits model assignments (Settings → AI → Models and Copilot Quick
// settings), so the draft/ack/retry machinery has one face. Returns true when the pickers must
// be read-only: no confirmed state yet, RPC missing, or a draft still awaiting its ack.
static bool drawModelRolesStatus(AppState& st) {
    if (s_roles.rolesDeferred)
        dimText(s_roles.rolesLoaded ? "Model assignments refresh when the current run finishes."
                                    : "Model assignments load when the current run finishes.");
    if (s_roles.rolesRpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("Model assignments are unavailable (read-only).");
        ImGui::PopStyleColor();
    }
    if (!s_roles.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_roles.lastError.c_str());
        {
            ImGuiContext& g = *GImGui;
            IMGUI_TEST_ENGINE_ITEM_INFO(0, s_roles.lastError.c_str(), g.LastItemData.StatusFlags);
        }
        ImGui::PopStyleColor();
    }
    const bool hasDraft = !s_roles.pendingPatch.is_null();
    if (hasDraft) {
        const char* status = s_roles.rolesSaveInFlight ? "Saving model assignment..." :
                             "Unsaved model assignment. Retry or discard this change.";
        dimText(status);
        {
            ImGuiContext& g = *GImGui;
            IMGUI_TEST_ENGINE_ITEM_INFO(0, status, g.LastItemData.StatusFlags);
        }
        if (!s_roles.rolesSaveInFlight) {
            if (ImGui::Button("Retry save")) sendPendingModelRoles(st);
            ImGui::SameLine();
            if (ImGui::Button("Discard change")) {
                s_roles.pendingPatch = nullptr;
                s_roles.lastError.clear();
                s_roles.lastRolesPoll = -1e9;
            }
        }
    }
    return s_roles.rolesRpcMissing || !s_roles.rolesLoaded || hasDraft ||
           s_roles.rolesSaveInFlight;
}

static const char* kSizeLabels[] = {"Small", "Medium", "Large"};

// Roles currently resolving to one size tier: a short "A, B, C +n" form and the full list.
static void modelSizeUsage(const char* key, std::string& usage, std::string& fullUsage) {
    int count = 0;
    for (int r = 0; r < kRoleCount; ++r) {
        auto it = s_roles.roleSizes.find(kRoleKeys[r]);
        if (it == s_roles.roleSizes.end() || it->second != key) continue;
        if (!fullUsage.empty()) fullUsage += ", ";
        fullUsage += kRoleLabels[r];
        if (count++ < 3) {
            if (!usage.empty()) usage += ", ";
            usage += kRoleLabels[r];
        }
    }
    if (count > 3) usage += " +" + std::to_string(count - 3);
}

// One size tier's confirmed assignment, or the in-flight draft over it.
static RoleAssign shownSizeAssign(const char* key) {
    RoleAssign fallback;
    fallback.mode = RoleMode::Default;
    auto it = s_roles.sizes.find(key);
    return modelRolesDraft("model_sizes", key, it == s_roles.sizes.end() ? fallback : it->second);
}

// One size tier's model picker over the shared confirmed/draft assignment. An explicit `width`
// places it inside a split row (see drawAssignmentPicker).
static void drawModelSizePicker(AppState& st, const char* key, bool ro,
                                const std::vector<RolePickEntry>& picks, float width = -1.0f) {
    drawAssignmentPicker(st, key, shownSizeAssign(key), picks, false, true, ro, false, width);
}

// The model whose reasoning ladder a tier's effort picker offers: the tier's pinned model, or,
// while it follows the chat model, the chat assignment's pin. Unknown when neither is pinned.
static void tierEffortModel(const RoleAssign& shown, std::string& provider, std::string& model) {
    const RoleAssign* pinned = shown.mode == RoleMode::Fixed ? &shown
        : shown.mode == RoleMode::Default && s_roles.chat.mode == RoleMode::Fixed ? &s_roles.chat
        : nullptr;
    if (!pinned) return;
    provider = pinned->provider;
    model = pinned->model;
}

// One size tier's thinking effort: Auto (the dispatching agent chooses per task) or a level the
// tier's model supports. Persisted on the tier's `model_sizes` entry.
static void drawModelSizeThinking(AppState& st, const char* key, bool ro, float width = -1.0f) {
    const RoleAssign shown = shownSizeAssign(key);
    std::string provider, model;
    tierEffortModel(shown, provider, model);
    RoleAssign next = shown;
    if (ro) ImGui::BeginDisabled();
    if (drawThinkingChoice("##size_think", provider, model, next.thinking, "Auto", width))
        setSizeAssign(st, key, next);
    if (ro) ImGui::EndDisabled();
}

// Settings layout: Size / usage | Model | Thinking table.
static void drawModelSizeTable(AppState& st, bool ro) {
    const auto& picks = buildRolePickList(st, /*localOnly=*/false, /*includeAuto=*/false);
    if (!ImGui::BeginTable("##model_sizes", 3,
                           ImGuiTableFlags_BordersInnerV | ImGuiTableFlags_RowBg |
                               ImGuiTableFlags_SizingStretchProp)) return;
    ImGui::TableSetupColumn("Size / usage", ImGuiTableColumnFlags_WidthStretch, 1.4f);
    ImGui::TableSetupColumn("Model", ImGuiTableColumnFlags_WidthStretch, 2.4f);
    ImGui::TableSetupColumn("Thinking", ImGuiTableColumnFlags_WidthFixed,
                            110.0f * ui::uiScale);
    ImGui::TableHeadersRow();
    for (int i = 0; i < 3; ++i) {
        const char* key = kSizeKeys[i];
        ImGui::PushID(key);
        ImGui::TableNextRow();
        ImGui::TableNextColumn();
        ImGui::TextUnformatted(kSizeLabels[i]);
        settingsAuditLastText(kSizeLabels[i]);
        std::string usage, fullUsage;
        modelSizeUsage(key, usage, fullUsage);
        if (!usage.empty()) {
            dimText(usage.c_str());
            if (usage != fullUsage && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", fullUsage.c_str());
        }
        ImGui::TableNextColumn();
        drawModelSizePicker(st, key, ro, picks);
        ImGui::TableNextColumn();
        drawModelSizeThinking(st, key, ro);
        ImGui::PopID();
    }
    ImGui::EndTable();
}

// Copilot Quick settings layout: caption left in the caller's shared column, model picker and
// thinking effort right; the controls drop under the caption (and the effort under the model)
// when the remainder is too narrow. The roles using a tier ride on the caption's tooltip.
// `captionInk` (0 = current text ink) tints the caption.
static void drawModelSizeRows(AppState& st, bool ro, float captionColumn, ImU32 captionInk) {
    const auto& picks = buildRolePickList(st, /*localOnly=*/false, /*includeAuto=*/false);
    for (int i = 0; i < 3; ++i) {
        const char* key = kSizeKeys[i];
        ImGui::PushID(key);
        const float x0 = ImGui::GetCursorScreenPos().x;
        const float avail = ImGui::GetContentRegionAvail().x;
        const float gap = ui::kit::padX();
        const float col = ImMin(captionColumn, avail * 0.45f);
        const float frameH = ImGui::GetFrameHeight();
        const bool sideBySide = avail - col - gap >= frameH * 7.0f;
        ImGui::AlignTextToFramePadding();
        if (captionInk) ImGui::PushStyleColor(ImGuiCol_Text, captionInk);
        ImGui::TextUnformatted(kSizeLabels[i]);
        if (captionInk) ImGui::PopStyleColor();
        settingsAuditLastText(kSizeLabels[i]);
        std::string usage, fullUsage;
        modelSizeUsage(key, usage, fullUsage);
        if (!fullUsage.empty() && ImGui::IsItemHovered())
            ui::kit::tooltip(("Used by: " + fullUsage).c_str());
        float regionW = avail;
        if (sideBySide) {
            ImGui::SameLine(0.0f, 0.0f);
            ImGui::SetCursorScreenPos(ImVec2(x0 + col + gap, ImGui::GetCursorScreenPos().y));
            regionW = avail - col - gap;
        }
        const float thinkW = ImClamp(regionW * 0.36f, 76.0f * ui::uiScale, 120.0f * ui::uiScale);
        const float modelW = regionW - thinkW - gap;
        if (modelW >= frameH * 6.0f) {
            drawModelSizePicker(st, key, ro, picks, modelW);
            ImGui::SameLine(0.0f, gap);
            drawModelSizeThinking(st, key, ro, thinkW);
        } else {
            drawModelSizePicker(st, key, ro, picks);
            drawModelSizeThinking(st, key, ro);
        }
        ImGui::PopID();
    }
}

static void drawModelRolesSection(AppState& st, float sc) {
    pollModelRoles(st, /*force=*/false);
    ensureDefaultRoles();

    dimText("Choose three model sizes for the team, each with its thinking effort, or turn on "
            "per-role overrides below. Unassigned sizes follow the chat model. "
            "Redaction remains local and is configured in Advanced overrides.");

    const bool ro = drawModelRolesStatus(st);
    const bool hasDraft = !s_roles.pendingPatch.is_null();
    ImGui::TextUnformatted("Chat model");
    settingsAuditLastText("Chat model");
    RoleAssign chat = s_roles.chat;
    if (hasDraft && s_roles.pendingPatch.contains("chat_assignment"))
        chat = roleAssignFromJson(s_roles.pendingPatch["chat_assignment"], chat.mode);
    const auto& chatPicks = buildRolePickList(st, false, false);
    ImGui::PushID("chat_assignment");
    drawAssignmentPicker(st, "chat", chat, chatPicks, false, false, ro, true);
    ImGui::PopID();
    drawModelSizeTable(st, ro);
    if (!ImGui::CollapsingHeader("Advanced overrides")) return;
    // Explicit opt-in. Off: every role dispatches through its tier above, and any saved
    // overrides below are kept but ignored.
    bool overridesOn = s_roles.advancedOverrides;
    if (hasDraft && s_roles.pendingPatch.contains("advanced_overrides") &&
        s_roles.pendingPatch["advanced_overrides"].is_boolean())
        overridesOn = s_roles.pendingPatch["advanced_overrides"].get<bool>();
    bool overridesToggle = overridesOn;
    if (ui::kit::checkbox("adv_overrides_on", "Use per-role overrides", &overridesToggle,
                          "Off: every role uses its tier (Small, Medium or Large). On: the "
                          "overrides below apply to their roles.", !ro))
        saveModelRoles(st, {{"advanced_overrides", overridesToggle}});
    dimText(!overridesOn
        ? "Off: every role uses its tier. Saved overrides below are kept but ignored."
        : s_roles.legacyMissionWorkerFallback
        ? "Legacy missions may use the worker override where shown; choose a tier to change it."
        : "Overrides apply only to the selected role; other roles use their tier.");

    const auto& allPicks = buildRolePickList(st, /*localOnly=*/false);
    const auto& localPicks = buildRolePickList(st, /*localOnly=*/true);

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
            RoleAssign a = modelRolesDraft("roles", key, s_roles.roles[key]);
            if (!isRedaction && !s_roles.activeOverrides.count(key) &&
                !(hasDraft && s_roles.pendingPatch.contains("roles") &&
                  s_roles.pendingPatch["roles"].contains(key)))
                a.mode = overridesOn && s_roles.legacyMissionWorkerFallback &&
                         !s_roles.legacyMissionFallbackExclusions.count(key)
                             ? RoleMode::LegacyWorker : RoleMode::Tier;
            // Following the chat model is no override: such a saved entry dispatches by tier.
            if (!isRedaction && a.mode == RoleMode::Default) a.mode = RoleMode::Tier;
            const auto& picks = isRedaction ? localPicks : allPicks;
            // Redaction is local-only policy, not a tier override, so the switch never gates it.
            const bool rowRo = ro || (!overridesOn && !isRedaction);

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
                helpMarker("Main copilot agent loop (Agent / Plan / Ask modes) when no more specific role applies.");
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
            drawAssignmentPicker(st, key, a, picks, isRedaction, false, rowRo);

            ImGui::TableNextColumn();
            // Effort follows the override's model; a tier reference uses that tier's model, and
            // a role on its tier takes the tier's effort (set above), so it has none of its own.
            std::string effortProvider = a.provider, effortModel = a.model;
            if (a.mode == RoleMode::Size)
                tierEffortModel(shownSizeAssign(a.size.c_str()), effortProvider, effortModel);
            const bool hasEffort = a.mode == RoleMode::Fixed || a.mode == RoleMode::Size;
            if (rowRo || !hasEffort) ImGui::BeginDisabled();
            RoleAssign next = a;
            if (drawThinkingChoice("##role_think", effortProvider, effortModel, next.thinking))
                setRoleAssign(st, key, next);
            if (rowRo || !hasEffort) ImGui::EndDisabled();

            // Redaction ITAR note lives on the Role [local] badge tooltip — never
            // TextWrapped inside the fixed-width Thinking column (that blew out the row).

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

}

// Copilot mode → tool policy + one-line description. Single source of truth for
// Settings → AI → Harness organization (index matches CopilotMode).
struct HarnessModeInfo {
    const char* label;       // Agent | Plan | Ask
    const char* structure;   // what one turn in this mode is
    const char* description; // one-line what it does
};
static const HarnessModeInfo kHarnessModes[] = {
    {"Agent",    "one agent-service turn",
     "Full CAD tool loadout; the agent decides inline vs subagents vs mission."},
    {"Plan",     "read-only planning",
     "Inspect and draft a plan; no mutation."},
    {"Ask",      "read-only Q&A",
     "Questions about the part; nothing changes."},
};
static constexpr int kHarnessModeCount =
    (int)(sizeof(kHarnessModes) / sizeof(kHarnessModes[0]));

// Settings → AI → Harness: mode grid + full quick-settings mirror (session knobs).
static void drawHarnessOrganizationSection(AppState& st, float sc);
bool        drawHarnessSessionKnobs(AppState& st, float sc);
static void drawMemoryLearningSection(AppState& st, float sc);


static void drawHarnessOrganizationSection(AppState& st, float sc) {
    // Mode chips in 2 columns (3 on wide panes): active mode accented, others secondary.
    dimText("Composer mode is a user choice (Agent / Plan / Ask); "
            "switch it from the copilot composer.");
    const int active = normalizeCopilotMode(st.settings.copilotMode);

    ImGui::Dummy(ImVec2(0, 2.0f * sc));
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(4.0f * sc, 1.5f * sc));
    const int modeCols = ImGui::GetContentRegionAvail().x >= 780.0f * sc ? 3 : 2;
    if (ImGui::BeginTable("##harness_modes", modeCols,
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
                const float* d = ui::palette.textDisabled;
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(d[0], d[1], d[2], d[3]));
                ImGui::TextUnformatted(line);
                ImGui::PopStyleColor();
            }
            if (ImGui::IsItemHovered() && m.description && m.description[0])
                ImGui::SetTooltip("%s", m.description);
        }
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();  // CellPadding
}

// Settings → AI → Harness: full mirror of copilot Quick settings + extras that
// only live in the modal (role adoption, privacy).
// Laid out in a 2-column grid to save vertical space.
bool drawHarnessSessionKnobs(AppState& st, float sc) {
    Settings& settings = st.settings;
    bool changed = false;
    // Progressive disclosure. This grid is shared with the AI workspace's Options view, so
    // gating here simplifies both surfaces from one place. Of its 36 controls the AI-tab
    // inventory classified 9 as everyday; the rest are orchestration-policy, budget,
    // transcript-density and experimental knobs that ship with correct defaults.
    const bool adv = st.settings.showAdvanced;
    // One cell-padding tier for every grid in this function (pref combos, checkboxes,
    // sliders, budgets) — the default 4x3dp table padding was the biggest vertical waste
    // in the modal.
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(4.0f * sc, 1.5f * sc));
    static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};
    // The preference combos and the budget sliders form one two-column grid: each table
    // column gets ONE label column sized to the widest caption stacked in it, so every combo
    // and slider down a column starts at the same x.
    const float harnessLeftColW =
        ImMax(ImMax(settingsLabelLeadWidth("Plan preference", true),
                    settingsLabelLeadWidth("Subagent preference", true)),
              ImMax(settingsLabelLeadWidth("Subagent tool-call budget", true),
                    settingsLabelLeadWidth("Project memory", true)));
    const float harnessRightColW =
        ImMax(ImMax(settingsLabelLeadWidth("Task list preference", true),
                    settingsLabelLeadWidth("Advisor preference", true)),
              settingsLabelLeadWidth("Max subagent nesting", true));

    auto prefCombo = [&](const char* label, const char* id, int* pref, const char* help,
                         float labelColW) {
        // Caption inline-left of the widget: one row per knob instead of caption+widget rows.
        // Every combo in a table column starts at that column's shared label edge.
        settingsLabelLead(label, help, labelColW);
        ImGui::SetNextItemWidth(-1.0f);
        int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
        if (settingsPopupRowCombo(id, &v, kPrefLabels, 4)) {
            *pref = v;
            if (pref == &settings.planPreference)
                settings.planMode = (v != 0);
            changed = true;
        }
    };

    // ---- Preferences (4 combos) — ADVANCED in full ----
    // Plan / task-list / subagent / advisor preference are agent-orchestration policy. The
    // shipped defaults already produce the normal behavior, and a user who has not read the
    // harness docs cannot tell Encourage from Require.
    if (adv) {
        if (ImGui::BeginTable("##harness_prefs", 2,
                              ImGuiTableFlags_SizingStretchSame |
                              ImGuiTableFlags_NoPadOuterX)) {
            ImGui::TableNextColumn();
            prefCombo("Plan preference", "##h_plan_pref", &settings.planPreference,
                      "Off = never plan first (default — agent tools directly). "
                      "Option = model may plan. "
                      "Encourage = plan by default. "
                      "Require = always draft a plan before building.",
                      harnessLeftColW);
            ImGui::TableNextColumn();
            prefCombo("Task list preference", "##h_tasklist_pref",
                      &settings.taskListPreference,
                      "Whether the agent publishes a live task list (the Tasks panel "
                      "in the copilot). Off = never. Option = model's choice. "
                      "Encourage = publish for any multi-step job. "
                      "Require = always publish before starting work.",
                      harnessRightColW);
            ImGui::TableNextColumn();
            prefCombo("Subagent preference", "##h_sub_pref", &settings.subagentPreference,
                      "How freely the main agent may spawn subagents. "
                      "Off disables nesting; Require pushes multi-agent splits.",
                      harnessLeftColW);
            ImGui::TableNextColumn();
            prefCombo("Advisor preference", "##h_adv_pref", &settings.advisorPreference,
                      "Off = no advisor. Option = only when the model asks. "
                      "Encourage = stronger guidance tone when proactive fires. "
                      "Require = more frequent checks when proactive is on.",
                      harnessRightColW);
            ImGui::EndTable();
        }
    }

    ImGui::Dummy(ImVec2(0, 6.0f * sc));

    // ---- Checkboxes in 2 columns (3 on wide panes) — mirrors quick-settings flags ----
    const int flagCols = ImGui::GetContentRegionAvail().x >= 780.0f * sc ? 3 : 2;
    if (ImGui::BeginTable("##harness_flags", flagCols,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        // Orchestration-topology flags: all ADVANCED. Each one changes how work is routed
        // (advisor injects) and ships off/lean, which is the correct normal configuration.
        if (adv) {
            ImGui::TableNextColumn();
            if (ui::kit::checkbox("h_proactive", "Proactive advisor", &settings.proactiveAdvisor))
                changed = true;
            helpMarker("Opt-in mid-run guidance on failure streaks and thrash. "
                       "Cheap deterministic notes by default; at most one short "
                       "advisor-model call on severe streaks (encourage/require). "
                       "Off = no proactive injects (pull request_advice still works).");
        }

        ImGui::TableNextColumn();
        if (ui::kit::checkbox("h_vision", "Vision (send screenshots)", &settings.vision,
                              "Attach viewport screenshots so the model can see the part."))
            changed = true;
        ImGui::TableNextColumn();
        if (ui::kit::checkbox("h_offline", "Offline mode (local only)", &settings.offlineMode,
                              "Refuses any cloud provider. Only a local loopback endpoint is "
                              "allowed."))
            changed = true;

        ImGui::TableNextColumn();
        ui::kit::checkbox("h_thoughts", "Show assistant thoughts", &ui::assistantThoughtsVisible(),
                          "Show the AI's reasoning steps inline in the conversation "
                          "(this session only).");

        ImGui::TableNextColumn();
        {
            bool redact = std::strcmp(settings.complianceMode, "redact") == 0;
            if (ui::kit::checkbox("h_redact", "Redact sensitive data (local, IP-safe)", &redact)) {
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
        if (adv) {
            ImGui::TableNextColumn();
            // Moved out of the retired Experimental tab: a modeling-behaviour preference,
            // so it belongs beside the other run preferences (and rides along into the AI
            // workspace's Options view, which shares this grid). ADVANCED: experimental.
            if (ui::kit::checkbox("h_sequence", "Editable feature sequence",
                                  &settings.expCadSequenceMode))
                changed = true;
            helpMarker(
                "Nudges the modeler to build the part as an ordered sequence of "
                "individually editable features (sketch -> extrude/revolve/loft/sweep -> "
                "fillet/chamfer/hole/pattern) that reconstructs a clean design history, "
                "rather than one fused monolithic solid \xe2\x80\x94 the DeepCAD / CAD-Coder "
                "command-sequence direction. Saved under config.json "
                "experimental.cad_sequence_mode.");
        }
        ImGui::EndTable();
    }

    ImGui::Dummy(ImVec2(0, 2.0f * sc));

    // ---- Chat display: how the transcript renders the agent's work. These knobs live in
    // config.json (agent.ui_chat_*) and persist themselves inside the ui/ai/ai_prefs.h setters,
    // so they deliberately do NOT flip `changed` — a saveSettings from here would write back
    // unrelated AppState::Settings state the caller never touched.
    //
    // Of the five, only "Show renders inline" is an everyday choice (whether you see the
    // images the model is looking at). The other four are transcript-density and diagnostic
    // formatting, so Basic shows the one and drops the section header with them.
    if (!adv) {
        bool renders = ui::chatShowRenders();
        if (ui::kit::checkbox("h_renders", "Show renders inline", &renders))
            ui::setChatShowRenders(renders);
        helpMarker("Display the images the vision model is looking at in the "
                   "transcript and the Artifacts panel. Off shows file paths instead.");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                     ui::palette.textDim[2], 1));
        ImGui::TextUnformatted("Chat display");
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * sc));

        if (ImGui::BeginTable("##harness_chat_display", flagCols,
                              ImGuiTableFlags_SizingStretchSame |
                              ImGuiTableFlags_NoPadOuterX)) {
            ImGui::TableNextColumn();
            {
                settingsLabelLead("Tool call detail",
                                  "Simplified: plain sentences only. Normal: sentences with an "
                                  "expandable raw drawer. Diagnostic: raw arguments, engine "
                                  "document ids and timings, drawers open by default.",
                                  0.0f);
                ImGui::SetNextItemWidth(-1.0f);
                static const char* kChatDetail[] = {"Simplified", "Normal", "Diagnostic"};
                int detail = ui::chatToolDetail();
                if (detail < 0 || detail > 2) detail = 1;
                if (settingsPopupRowCombo("##h_chat_detail", &detail, kChatDetail, 3))
                    ui::setChatToolDetail(detail);
            }

            ImGui::TableNextColumn();
            {
                bool anim = ui::chatCollapseAnim();
                if (ui::kit::checkbox("h_collapse_anim", "Animate collapsing boxes", &anim))
                    ui::setChatCollapseAnim(anim);
                helpMarker("Thinking boxes and tool drawers slide shut when the next step "
                           "starts instead of snapping.");
            }

            ImGui::TableNextColumn();
            {
                bool group = ui::chatGroupToolCalls();
                if (ui::kit::checkbox("h_group_calls", "Group repeated tool calls", &group))
                    ui::setChatGroupToolCalls(group);
                helpMarker("Fold consecutive calls of the same tool into one card with a "
                           "count. Failures are never folded.");
            }

            ImGui::TableNextColumn();
            {
                bool renders = ui::chatShowRenders();
                if (ui::kit::checkbox("h_renders", "Show renders inline", &renders))
                    ui::setChatShowRenders(renders);
                helpMarker("Display the images the vision model is looking at in the "
                           "transcript and the Artifacts panel. Off shows file paths "
                           "instead.");
            }

            ImGui::TableNextColumn();
            {
                bool human = ui::chatHumanizeDocIds();
                if (ui::kit::checkbox("h_humanize", "Humanize document ids", &human))
                    ui::setChatHumanizeDocIds(human);
                helpMarker("Replace engine ids like doc-cpp0000001f with the part's name "
                           "in agent text. Turn off to see exactly what the model sent.");
            }
            ImGui::EndTable();
        }
    }

    // ---- Sliders / budgets — ADVANCED in full ----
    // Subagent tool-call budget, nesting depth and memory token budget are all convergence
    // and prompt-budget tuning. Each already self-tunes ("automatic" / shipped cap), and a
    // wrong value here degrades runs in ways that are hard to attribute. This table is the
    // last thing the function draws, so Basic mode simply stops here.
    if (!adv) {
        ImGui::PopStyleVar();  // CellPadding
        return changed;
    }

    ImGui::Dummy(ImVec2(0, 2.0f * sc));

    if (ImGui::BeginTable("##harness_sliders", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        {
            // Shared cache (ui::loadSubagentToolCallBudget): refreshed by its saver, so a change
            // made anywhere shows here on the next frame instead of after a restart.
            const int toolCallBudget = ui::loadSubagentToolCallBudget();
            settingsLabelLead("Subagent tool-call budget",
                              "A delegated subagent gets a soft warning to converge at this "
                              "many tool calls. Leave empty (Auto) to derive it from the "
                              "subagent's token grant. The main chat has no tool-call budget. "
                              "Saved to config.json agent.subagent_tool_call_budget.",
                              harnessLeftColW);
            // 0 = automatic: shown as an empty field with an "Auto" hint, never as a zero.
            bool manual = toolCallBudget > 0;
            int budget = manual ? toolCallBudget : 16;
            if (ui::kit::fieldInt("h_subagent_calls", "", &budget,
                                  ui::kit::FieldOpts()
                                      .withPresent(&manual)
                                      .withHint("Auto")
                                      .withRange(1, 64)
                                      .withTip("Tool calls before a subagent is told to "
                                               "converge (1-64). Empty = Auto."))) {
                ui::saveSubagentToolCallBudget(manual ? budget : 0);
            }
        }

        ImGui::TableNextColumn();
        {
            // Shared cache (ui::loadMaxSubagentDepth) — Quick settings reads the same one.
            const int maxDepth = ui::loadMaxSubagentDepth();
            settingsLabelLead("Max subagent nesting",
                              "Nesting levels of subagent-spawning-subagent (not concurrency). "
                              "Root chat = depth 0. Saved to config.json "
                              "agent.max_subagent_depth.",
                              harnessRightColW);
            static const char* kDepthLabels[] = {"0", "1", "2", "3", "4"};
            static const char* kDepthTips[] = {
                "Subagents cannot spawn subagents.",
                "Subagents may spawn one level of helpers.",
                "Two nested levels below the main chat.",
                "Three nested levels below the main chat.",
                "Four nested levels below the main chat."};
            int depth = ImClamp(maxDepth, 0, 4);
            if (ui::kit::segmented("##h_depth", kDepthLabels, 5, &depth,
                                   ImGui::GetContentRegionAvail().x, kDepthTips)) {
                ui::saveMaxSubagentDepth(depth);
            }
        }


        ImGui::EndTable();
    }

    ImGui::PopStyleVar();  // CellPadding
    return changed;
}

// Settings -> AI -> Roles: the command-model / vision-model split (Seek-CAD style). Moved here
// from the retired Experimental tab — it IS a model-role assignment, so it belongs beside the
// other per-role pickers rather than in a separate tab.
static bool drawVisionRoutingSection(AppState& st, float sc) {
    Settings& settings = st.settings;
    bool changed = false;  // caller saves (every control here edits Settings::exp*)

    // Command model = the active main provider/model (emits + parses the CAD ops). Chosen in
    // Settings -> Providers / the composer's model chip; shown read-only so both routes are
    // visible side by side. The cloud-compat slot hosts many services, so name the SERVICE
    // (Moonshot / DeepSeek / ...) instead of the generic "OpenRouter / Cloud API" slot label.
    ImGui::TextUnformatted("Command model (sends & parses CAD ops)");
    helpMarker("The model that drives the agent loop: understands the request, plans, and "
               "emits/parses the CAD operations. Pick it in Settings -> Providers or the "
               "composer's model chip. Shown here so you can see both routes side by side.");
    {
        const char* provLabel = settings.ompProvider.empty()
            ? ui::providerLabelForEndpoint(settings.provider, settings.baseUrl)
            : settings.ompProvider.c_str();
        char cm[600];
        std::snprintf(cm, sizeof(cm), "   %s  \xc2\xb7  %s", provLabel,
                      settings.model[0] ? settings.model : "(default)");
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.accent[0], ui::palette.accent[1],
                                                    ui::palette.accent[2], 1));
        ImGui::TextUnformatted(cm);
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 6 * sc));
    if (ui::kit::checkbox("vis_route", "Route vision & spatial reasoning through a separate model",
                          &settings.expVisionReview))
        changed = true;
    helpMarker("When on, a vision model judges the rendered viewport during the review/fix "
               "stages (spatial awareness), while the command model above plans and emits ops "
               "— the Seek-CAD split. It can be local (ITAR-safe) or a cloud vision model. "
               "When off, the command model handles vision too (if it supports images). "
               "Refused under Offline mode if cloud.");
    dimText("Command model emits ops; this vision model sees the screenshot and drives fixes.");
    if (!settings.expVisionReview) return changed;

    ImGui::Dummy(ImVec2(0, 2 * sc));
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
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Vision / spatial model");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(-1);
    if (settingsPopupRowCombo("##expvispreset", &vsel, vlabels.data(),
                              (int)vlabels.size()) && vsel > 0 &&
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
        changed = true;
    }
    // Model id + endpoint side by side. Credential entry is intentionally absent: provider
    // credentials are managed by Omp under Settings -> Providers.
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(4.0f * sc, 1.5f * sc));
    if (ImGui::BeginTable("##expvis_fields", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Model id");
        ImGui::SameLine();
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##expvismodel", settings.expVisionModel,
                             sizeof(settings.expVisionModel)))
            changed = true;
        ImGui::TableNextColumn();
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("Endpoint (base URL)");
        ImGui::SameLine();
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##expvisbase", settings.expVisionBaseUrl,
                             sizeof(settings.expVisionBaseUrl)))
            changed = true;
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();  // CellPadding
    ImGui::AlignTextToFramePadding();
    if (ui::baseUrlIsLoopback(settings.expVisionBaseUrl)) {
        dimText("Local loopback endpoint — no API key required.");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped("Cloud credentials are managed under Settings \xe2\x86\x92 Providers. "
                           "This override does not accept a key.");
        ImGui::PopStyleColor();
    }
    return changed;
}

// ---- agent.* memory/learning knobs (config.json) -----------------------------
// Same load-modify-write recipe as ui::loadMaxSubagentDepth/saveMaxSubagentDepth
// (copilot/settings.cpp): read with a default, save through updateConfigJson().
static std::string loadAgentConfigString(const char* key, const char* fallback) {
    std::string v = fallback;
    try {
        const nlohmann::json j = readConfigJson();
        auto agent = j.value("agent", nlohmann::json::object());
        if (agent.is_object() && agent.contains(key) && agent[key].is_string())
            v = agent[key].get<std::string>();
    } catch (...) {}
    return v;
}
static bool loadAgentConfigBool(const char* key, bool fallback) {
    bool v = fallback;
    try {
        const nlohmann::json j = readConfigJson();
        auto agent = j.value("agent", nlohmann::json::object());
        if (agent.is_object() && agent.contains(key) && agent[key].is_boolean())
            v = agent[key].get<bool>();
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
        const nlohmann::json j = readConfigJson();
        auto agent = j.value("agent", nlohmann::json::object());
        if (agent.is_object() && agent.contains(obj) && agent[obj].is_object() &&
            agent[obj].contains(key) && agent[obj][key].is_boolean())
            v = agent[obj][key].get<bool>();
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

    static bool loaded = false;
    static std::string strategy;
    static bool compactionApplying = false;
    static std::string compactionStatus;
    static std::string compactionEffective;
    static bool s_autolearn = false;
    static bool s_autoContinue = false;
    if (!loaded) {
        loaded = true;
        strategy = loadAgentConfigString("compaction_strategy", "auto");
        s_autolearn = loadAgentConfigBool("autolearn_enabled", false);
        s_autoContinue = loadAgentConfigBool("autolearn_auto_continue", false);
    }
    static const char* backends[] = {"off", "sharpshooter", "mnemopi", "local", "hindsight"};
    static const char* backendLabels[] = {"Off", "Decisions", "Full recall", "Local", "Hindsight"};
    int backend = -1;
    for (int i = 0; i < 5; ++i)
        if (st.settings.memoryBackend == backends[i]) backend = i;
    ImGui::TextUnformatted("Project memory");
    settingsAuditLastText("Project memory");
    const char* memoryPreview = backend >= 0 ? backendLabels[backend] : st.settings.memoryBackend.c_str();
    if (settingsBeginPopupRowCombo("##memory_backend", memoryPreview, 220.0f * sc,
                                   ImGuiComboFlags_HeightLarge)) {
        for (int i = 0; i < 5; ++i) {
            if (settingsPopupRowSelectable(backendLabels[i], backend == i)) {
                st.settings.memoryBackend = backends[i];
                st.settings.memoryEnabled = i != 0;
                saveSettings(st.settings);
            }
        }
        settingsEndPopupRowCombo();
    }
    dimText("Memory is scoped to the project. Backends may require additional setup.");

    static const char* modes[] = {"auto", "off", "snapcompact", "shake", "summary"};
    static const char* labels[] = {"Automatic", "Off", "Snapcompact", "Shake", "Summary"};
    int selected = -1;
    for (int i = 0; i < 5; ++i) if (strategy == modes[i]) selected = i;
    ImGui::TextUnformatted("Compaction (next turn)");
    settingsAuditLastText("Compaction (next turn)");
    const std::string preview = selected < 0 ? strategy + " (unsupported)" : labels[selected];
    const bool compactionDisabled = compactionApplying;
    if (compactionDisabled) ImGui::BeginDisabled();
    if (settingsBeginPopupRowCombo("##comp_mode", preview.c_str(), 220.0f * sc,
                                   ImGuiComboFlags_HeightLarge)) {
        for (int i = 0; i < 5; ++i) {
            if (settingsPopupRowSelectable(labels[i], selected == i)) {
                const std::string next = modes[i];
                const bool saved = updateConfigJson([&](nlohmann::json& config) {
                    if (!config.contains("agent") || !config["agent"].is_object())
                        config["agent"] = nlohmann::json::object();
                    config["agent"]["compaction_strategy"] = next;
                });
                if (!saved || !flushConfigWrites()) {
                    compactionStatus = "Could not save compaction policy; retry the selection.";
                } else {
                    strategy = next;
                    compactionEffective.clear();
                    const char* shot = std::getenv("CHUDWARE_SHOT");
                    const bool fixture = st.shot && shot && std::strcmp(shot, "model_settings") == 0;
                    if (fixture) {
                        // The isolated visual fixture has no runtime sessions to mutate.
                        compactionStatus = "Policy saved for future sessions; no live application reported.";
                    } else {
                        compactionApplying = true;
                        compactionStatus = "Applying compaction policy...";
                        st.rpc.call("omp_apply_compaction_policy", nlohmann::json{{"strategy", next}},
                                [](const core::RpcResult& r) {
                        compactionApplying = false;
                        if (!r.ok || !r.result.is_object() ||
                            !optionalJsonBool(r.result, "ok", false)) {
                            compactionStatus = "Policy saved, but live application failed; retry or use a new session.";
                            return;
                        }
                        const auto* sessions = optionalJsonArr(r.result, "sessions");
                        if (!sessions || sessions->empty()) {
                            compactionStatus = "Policy saved for future sessions; no live application reported.";
                            return;
                        }
                        std::string sharedOrder;
                        bool sameOrder = true;
                        for (const auto& session : *sessions) {
                            if (!session.is_object() || !optionalJsonBool(session, "ok", false)) {
                                compactionStatus = "Policy saved, but at least one session did not apply it.";
                                return;
                            }
                            const auto effective = session.find("effective");
                            if (effective == session.end() || !effective->is_object()) {
                                sameOrder = false;
                                continue;
                            }
                            const auto compaction = effective->find("compaction");
                            if (compaction == effective->end() || !compaction->is_object()) {
                                sameOrder = false;
                                continue;
                            }
                            const auto order = compaction->find("methodOrder");
                            if (order == compaction->end() || !order->is_array()) {
                                sameOrder = false;
                                continue;
                            }
                            std::string value;
                            for (const auto& method : *order) {
                                if (!method.is_string()) { sameOrder = false; break; }
                                if (!value.empty()) value += ", ";
                                value += method.get<std::string>();
                            }
                            if (value.empty()) { sameOrder = false; continue; }
                            if (sharedOrder.empty()) sharedOrder = value;
                            else if (sharedOrder != value) sameOrder = false;
                        }
                        if (sameOrder && !sharedOrder.empty())
                            compactionEffective = "Reported effective order: " + sharedOrder;
                        compactionStatus = "Policy saved and acknowledged by the reported sessions.";
                    }, 30000);
                    }
                }
            }
        }
        settingsEndPopupRowCombo();
    }
    if (compactionDisabled) ImGui::EndDisabled();
    if (!compactionStatus.empty()) settingsAuditedDimText(compactionStatus.c_str());
    if (strategy == "auto")
        dimText(compactionEffective.empty() ? "Effective runtime order is not yet available."
                                            : compactionEffective.c_str());
    if (strategy == "snapcompact") {
        const std::string provider = !st.settings.composerFollowRoles ? st.settings.ompProvider : s_roles.chat.provider;
        const std::string model = !st.settings.composerFollowRoles ? st.settings.model : s_roles.chat.model;
        const auto* meta = ui::bridgeModelMeta(provider, model);
        if (meta && meta->visionKnown && !meta->vision)
            settingsAuditedDimText("Unavailable for this model: image input required. The selection is preserved.");
        else if (!meta || !meta->visionKnown)
            settingsAuditedDimText("Snapcompact requires image input; model capability is unknown.");
    }
    settingsSectionHeader("Active agent session");
    const auto* link = ui::ompSessionLink(st.activeConvId());
    if (!link || !link->runtimeLive)
        settingsAuditedDimText("No live agent session. Session controls are unavailable.");
    else
        settingsAuditedDimText("Current session compaction and retry state unavailable.");
    if (!st.settings.showAdvanced) return;

    // ---- Auto-Learn (experimental) ----
    settingsSectionHeader("Auto-Learn (experimental)");
    dimText("After substantive turns, capture lessons into project memory. Standing "
            "guidance is prompt-cache neutral; auto-run capture uses an extra private "
            "write when enabled.");
    if (ImGui::BeginTable("##autolearn", 2,
                          ImGuiTableFlags_SizingStretchSame |
                          ImGuiTableFlags_NoPadOuterX)) {
        ImGui::TableNextColumn();
        if (ui::kit::checkbox("mem_autolearn", "Auto-Learn (experimental)", &s_autolearn))
            saveAgentConfigValue("autolearn_enabled", s_autolearn);
        helpMarker("After the agent stops, nudge it to capture lessons to memory "
                   "(agent.autolearn_enabled). Standing guidance is injected into "
                   "the system prompt when on. Default off.");
        ImGui::TableNextColumn();
        if (!s_autolearn) ImGui::BeginDisabled();
        if (ui::kit::checkbox("mem_autocontinue", "Auto-run capture at stop", &s_autoContinue))
            saveAgentConfigValue("autolearn_auto_continue", s_autoContinue);
        helpMarker("When on, auto-write one project-memory note after a turn with "
                   "enough tool calls (agent.autolearn_auto_continue). Uses a little "
                   "extra work. When off, only standing Auto-Learn guidance remains.");
        if (!s_autolearn) ImGui::EndDisabled();

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
            // Read the shared getters every frame (cached in ai_prefs, one config read per
            // process) so a change made in AI > Options shows here immediately.
            int verbosity = ui::aiUiVerbosity();
            settingsLabelLead(ui::aiPrefCopy::kDetailLabel, ui::aiPrefCopy::kDetailHelp,
                              settingsLabelLeadWidth(ui::aiPrefCopy::kDetailLabel, true));
            ImGui::SetNextItemWidth(-1.0f);
            if (settingsPopupRowCombo("##ws_verbosity", &verbosity,
                                      ui::aiPrefCopy::kDetailLevels, 3))
                ui::setAiUiVerbosity(verbosity);
        }
        ImGui::TableNextColumn();
        bool follow = ui::aiFollowActiveSession();
        if (ui::kit::checkbox("mem_follow", ui::aiPrefCopy::kFollowLabel, &follow))
            ui::setAiFollowActiveSession(follow);
        helpMarker(ui::aiPrefCopy::kFollowHelp);

        ImGui::TableNextColumn();
        bool previews = ui::aiActiveWorkPreviews();
        if (ui::kit::checkbox("mem_previews", ui::aiPrefCopy::kPreviewsLabel, &previews))
            ui::setAiActiveWorkPreviews(previews);
        helpMarker(ui::aiPrefCopy::kPreviewsHelp);
        ImGui::EndTable();
    }
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
    bool bodyTruncated = false;    // source exceeded the editor buffer: never write it back
    uint64_t bodyReq = 0;          // newest skills_get request; older replies are dropped
    uint64_t editSeq = 0;          // bumped by every body edit (guards stale save acks)
    uint64_t autosaveSeq = 0;      // editSeq an autosave last tried: failed text waits for a new edit
    double   lastEdit = 0.0;       // ImGui::GetTime() of the last body edit
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
    if (name.empty()) return;
    // Latest selection wins: a superseded reply is dropped by its request id instead of the new
    // selection being ignored while an older one loads.
    const uint64_t req = ++s_ovSkills.bodyReq;
    s_ovSkills.bodyInFlight = true;
    s_ovSkills.bodyName.clear();  // the buffer belongs to no skill until its body arrives
    s_ovSkills.bodyDirty = false;
    s_ovSkills.bodyTruncated = false;
    std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
    st.rpc.call("skills_get", nlohmann::json{{"name", name}},
                [name, req](const core::RpcResult& r) {
        if (req != s_ovSkills.bodyReq) return;  // superseded by a newer selection
        s_ovSkills.bodyInFlight = false;
        if (ovSkillsAbsent(r)) {
            s_ovSkills.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovSkills.lastError = r.err.empty()
                ? r.result.value("reason", std::string("skills_get failed"))
                : r.err;
            if (s_ovSkills.selected == name) s_ovSkills.selected.clear();  // no blank editor to save over it
            return;
        }
        if (s_ovSkills.selected != name) return;  // selection moved on
        std::string body = r.result.value("body", std::string());
        std::fill(s_ovSkills.body.begin(), s_ovSkills.body.end(), 0);
        s_ovSkills.bodyTruncated = body.size() >= s_ovSkills.body.size();
        if (s_ovSkills.bodyTruncated)
            body.resize(s_ovSkills.body.size() - 1);
        std::memcpy(s_ovSkills.body.data(), body.data(), body.size());
        s_ovSkills.bodyName = name;
        s_ovSkills.bodyDirty = false;
        s_ovSkills.lastError = s_ovSkills.bodyTruncated
            ? std::string("This skill is larger than the editor buffer; it is shown truncated "
                          "and will not be saved from here.")
            : std::string();
    });
}
static void ovSaveSkill(AppState& st) {
    // Only the selected skill's fully loaded body may be written: a buffer that is still loading,
    // failed to load or was truncated would overwrite the real skill with blank/partial text.
    if (s_ovSkills.selected.empty() || s_ovSkills.saveInFlight || s_ovSkills.bodyInFlight ||
        s_ovSkills.bodyName != s_ovSkills.selected || s_ovSkills.bodyTruncated)
        return;
    s_ovSkills.saveInFlight = true;
    const uint64_t editSeq = s_ovSkills.editSeq;
    nlohmann::json p;
    p["name"] = s_ovSkills.selected;
    p["body"] = std::string(s_ovSkills.body.data());
    st.rpc.call("skills_save", p, [editSeq](const core::RpcResult& r) {
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
        // Keystrokes made while this save was in flight stay pending.
        if (editSeq == s_ovSkills.editSeq) s_ovSkills.bodyDirty = false;
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

// Skill editor autosave (an existing skill only — creating one stays an explicit Create): a body
// edit is committed once typing pauses, and forced before the selection changes or the modal
// closes, so no edit is ever silently discarded.
static void flushSkillEdits(AppState& st, bool force) {
    OvSkillsUi& s = s_ovSkills;
    if (!s.bodyDirty || s.creating || s.rpcMissing || s.selected.empty()) return;
    if (s.bodyInFlight || s.saveInFlight || s.bodyName != s.selected || s.bodyTruncated) return;
    // `force` (selection change / close) skips only the typing pause. Each edit is attempted once:
    // forcing every frame while the modal is closed would re-send a refused text forever.
    if (!force && ImGui::GetTime() - s.lastEdit < 1.5) return;
    if (s.autosaveSeq == s.editSeq) return;  // this exact text was already tried
    s.autosaveSeq = s.editSeq;
    // The worker refuses a blank skill; wait for text instead of erroring on every pause.
    if (std::strspn(s.body.data(), " \t\r\n") == std::strlen(s.body.data())) return;
    ovSaveSkill(st);
}

// ---- Rules: context-injection triggers (Settings → AI → Rules). Wire names stay triggers_*. ----
// A text field this tab both DISPLAYS and SAVES, where the text came from Python.
//
// The UI font atlas has no glyph for the Greek notation the shipped context packs use (ε strain,
// δ deflection, θ bend angle), so everything shown must go through ui::foldToAtlasGlyphs first —
// otherwise the pane is a wall of '?'. But those packs are MODEL-facing prose injected into the
// prompt, so the fold must never travel back to disk. Hence: `raw` is the verbatim source,
// `shown` is byte-for-byte what load() put in the ImGui buffer, and save() writes `raw` back for
// any buffer the user left alone.
struct OvRawField {
    std::string raw;     // verbatim, as triggers_get returned it
    std::string shown;   // folded (and buffer-truncated) — what the user is looking at

    void load(const std::string& src, char* buf, size_t n) {
        raw = src;
        shown = ui::foldToAtlasGlyphs(src);
        if (n == 0) { shown.clear(); return; }
        if (shown.size() >= n) {
            // Trim to a UTF-8 boundary: folding leaves covered multi-byte glyphs (× —) in place
            // and half a sequence would render as the very '?' this exists to prevent.
            size_t cut = n - 1;
            while (cut > 0 && (static_cast<unsigned char>(shown[cut]) & 0xC0) == 0x80) --cut;
            shown.resize(cut);
        }
        std::memset(buf, 0, n);
        std::memcpy(buf, shown.data(), shown.size());
    }
    void load(const std::string& src, std::vector<char>& buf) {
        load(src, buf.data(), buf.size());
    }
    // Reset for a field with no source behind it (the "New rule" templates are C++ ASCII).
    void clear() { raw.clear(); shown.clear(); }
    std::string save(const char* buf) const {
        std::string cur(buf);
        return cur == shown ? raw : cur;
    }
};

struct OvTriggerRow {
    std::string id;
    std::string label;    // human title from triggers_list ("thread_mention" -> "Thread mention")
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
    // Every editor buffer above shows text FOLDED for the atlas (see ui::foldToAtlasGlyphs), but
    // the sources are model-facing: a shipped pack must not lose its Greek notation just because
    // the user opened this tab. Each field therefore remembers both the verbatim source and the
    // exact folded text put in the buffer — see OvRawField.
    OvRawField purposeSrc, packNameSrc, exemplarSrc, keywordsSrc, toolsSrc, eventsSrc,
        packBodySrc;
    bool bodyDirty = false;   // any editor field dirty
    bool packDirty = false;   // pack body specifically edited (avoid freezing shipped packs)
    nlohmann::json prerunCalcs = nlohmann::json::array();  // preserved from get/list
    bool creating = false;
    char newId[96] = {};
    std::string bodyId;           // rule the editor buffers belong to ("" while loading / failed)
    uint64_t bodyReq = 0;         // newest triggers_get request; older replies are dropped
    uint64_t editSeq = 0;         // bumped by every edit (guards stale save acks)
    uint64_t autosaveSeq = 0;     // editSeq an autosave last tried: refused text waits for a new edit
    double   lastEdit = 0.0;      // ImGui::GetTime() of the last edit (-1e9 = commit on the next frame)
};
static OvTriggersUi s_ovTrig;
// Every editor field funnels through here so the debounced autosave (flushTriggerEdits) sees it.
// `immediate` (checkbox) skips the typing pause.
static void ovTrigEdited(bool immediate = false) {
    s_ovTrig.bodyDirty = true;
    ++s_ovTrig.editSeq;
    s_ovTrig.lastEdit = immediate ? -1e9 : ImGui::GetTime();
}
// Guided rule/skill/context authoring popup (opened by "New rule…" in the Rules tab).
static bool s_rulesAuthorOpen = false;

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
// One line per entry. Returned rather than written into a buffer: the caller hands it to an
// OvRawField, which owns the folding and the buffer truncation.
static std::string ovJoinLines(const std::vector<std::string>& xs) {
    std::string s;
    for (size_t i = 0; i < xs.size(); ++i) {
        if (i) s.push_back('\n');
        s += xs[i];
    }
    return s;
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
                // RulesEngineLive serves `label` on every row; an older user override written
                // before that may not have it, and an empty heading is worse than the raw id.
                row.label = e.value("label", std::string());
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
    if (id.empty()) return;
    // Latest selection wins (see ovLoadSkillBody): a superseded reply is dropped by request id.
    const uint64_t req = ++s_ovTrig.bodyReq;
    s_ovTrig.bodyInFlight = true;
    s_ovTrig.bodyId.clear();  // the buffers belong to no rule until its body arrives
    s_ovTrig.bodyDirty = false;
    s_ovTrig.packDirty = false;
    st.rpc.call("triggers_get", nlohmann::json{{"id", id}},
                [id, req](const core::RpcResult& r) {
        if (req != s_ovTrig.bodyReq) return;  // superseded by a newer selection
        s_ovTrig.bodyInFlight = false;
        if (ovTriggersAbsent(r)) {
            s_ovTrig.rpcMissing = true;
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            s_ovTrig.lastError = r.err.empty()
                ? r.result.value("reason", std::string("triggers_get failed"))
                : r.err;
            if (s_ovTrig.selected == id) s_ovTrig.selected.clear();  // no stale-field editor to save
            return;
        }
        if (s_ovTrig.selected != id) return;
        auto trig = r.result.value("trigger", nlohmann::json::object());
        auto pack = r.result.value("pack", nlohmann::json::object());
        s_ovTrig.enabled = trig.value("enabled", true);
        s_ovTrig.budgetTokens = trig.value("budget_tokens", 250);
        s_ovTrig.cooldownTurns = trig.value("cooldown_turns", 8);
        s_ovTrig.purposeSrc.load(trig.value("purpose", std::string()),
                                 s_ovTrig.purpose, sizeof(s_ovTrig.purpose));
        auto inject = trig.value("inject", nlohmann::json::object());
        std::string packName = inject.is_object()
            ? inject.value("context_pack", std::string()) : std::string();
        std::string exemplar = inject.is_object()
            ? inject.value("exemplar", std::string()) : std::string();
        s_ovTrig.prerunCalcs = (inject.is_object() && inject.contains("prerun_calcs")
                                && inject["prerun_calcs"].is_array())
            ? inject["prerun_calcs"] : nlohmann::json::array();
        s_ovTrig.packNameSrc.load(packName, s_ovTrig.packName, sizeof(s_ovTrig.packName));
        s_ovTrig.exemplarSrc.load(exemplar, s_ovTrig.exemplar, sizeof(s_ovTrig.exemplar));
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
        s_ovTrig.keywordsSrc.load(ovJoinLines(kws), s_ovTrig.keywords,
                                  sizeof(s_ovTrig.keywords));
        s_ovTrig.toolsSrc.load(ovJoinLines(tools), s_ovTrig.tools, sizeof(s_ovTrig.tools));
        s_ovTrig.eventsSrc.load(ovJoinLines(evs), s_ovTrig.events, sizeof(s_ovTrig.events));
        s_ovTrig.packBodySrc.load(
            pack.is_object() ? pack.value("body", std::string()) : std::string(),
            s_ovTrig.packBody);
        s_ovTrig.bodyDirty = false;
        s_ovTrig.packDirty = false;
        s_ovTrig.bodyId = id;
        s_ovTrig.lastError.clear();
    });
}
static void ovSaveTrigger(AppState& st) {
    // Never write buffers that do not hold the selected rule's loaded body (still loading, or its
    // load failed): that would graft the previous rule's fields onto this id.
    if (s_ovTrig.selected.empty() || s_ovTrig.saveInFlight || s_ovTrig.bodyInFlight ||
        s_ovTrig.bodyId != s_ovTrig.selected)
        return;
    s_ovTrig.saveInFlight = true;
    const uint64_t editSeq = s_ovTrig.editSeq;
    nlohmann::json p;
    p["id"] = s_ovTrig.selected;
    p["enabled"] = s_ovTrig.enabled;
    // Each field: the verbatim source when the user left the (folded) buffer alone, otherwise
    // exactly what they typed. Merely opening a shipped pack must not rewrite it as ASCII.
    p["purpose"] = s_ovTrig.purposeSrc.save(s_ovTrig.purpose);
    p["budget_tokens"] = s_ovTrig.budgetTokens;
    p["cooldown_turns"] = s_ovTrig.cooldownTurns;
    p["match"] = nlohmann::json{
        {"keywords", ovSplitLinesOrCommas(
             s_ovTrig.keywordsSrc.save(s_ovTrig.keywords).c_str())},
        {"tools", ovSplitLinesOrCommas(s_ovTrig.toolsSrc.save(s_ovTrig.tools).c_str())},
        {"events", ovSplitLinesOrCommas(s_ovTrig.eventsSrc.save(s_ovTrig.events).c_str())},
    };
    p["inject"] = nlohmann::json{
        {"context_pack", s_ovTrig.packNameSrc.save(s_ovTrig.packName)},
        {"prerun_calcs", s_ovTrig.prerunCalcs.is_array()
            ? s_ovTrig.prerunCalcs : nlohmann::json::array()},
        {"exemplar", s_ovTrig.exemplarSrc.save(s_ovTrig.exemplar)},
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
        p["pack_body"] = s_ovTrig.packBodySrc.save(s_ovTrig.packBody.data());
        p["pack_name"] = s_ovTrig.packNameSrc.save(s_ovTrig.packName);
    }
    st.rpc.call("triggers_save", p, [editSeq](const core::RpcResult& r) {
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
        s_ovTrig.loaded = false;
        s_ovTrig.lastList = -1e9;
        // The rule saved but its context pack may not have: surface that, and keep the pack
        // edit pending instead of reporting a clean save.
        if (r.result.is_object() && !r.result.value("pack_ok", true)) {
            s_ovTrig.lastError = r.result.value("pack_reason", std::string("pack save failed"));
            return;
        }
        // Keystrokes made while this save was in flight stay pending.
        if (editSeq == s_ovTrig.editSeq) {
            s_ovTrig.bodyDirty = false;
            s_ovTrig.packDirty = false;
        }
        s_ovTrig.lastError.clear();
    });
}
// Rule editor autosave — same contract as flushSkillEdits: existing rules only (a new rule stays
// an explicit Create because it must be named first).
static void flushTriggerEdits(AppState& st, bool force) {
    OvTriggersUi& t = s_ovTrig;
    if (!t.bodyDirty || t.creating || t.rpcMissing || t.selected.empty()) return;
    if (t.bodyInFlight || t.saveInFlight || t.bodyId != t.selected) return;
    if (!force && ImGui::GetTime() - t.lastEdit < 1.5) return;
    if (t.autosaveSeq == t.editSeq) return;  // this exact text was already tried
    t.autosaveSeq = t.editSeq;
    ovSaveTrigger(st);
}
// Debounced autosave of the Skills + Rules editors (RPC-backed): called every frame from
// drawSettingsModal and forced on close / before the selection changes / by
// flushSettingsModalRpcEdits at app exit (while the RPC layer is still up).
static void flushSkillRuleEdits(AppState& st, bool force) {
    flushSkillEdits(st, force);
    flushTriggerEdits(st, force);
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
    if (ui::InterwebzGroupBegin("##set_skill_list", "Skills", ImVec2(0, colH))) {
        int userSkills = 0;
        int projectSkills = 0;
        for (const auto& sk : s_ovSkills.skills) {
            if (sk.source == "user") ++userSkills;
            else if (sk.source == "project") ++projectSkills;
        }
        std::string catalogMeta = std::to_string(s_ovSkills.skills.size()) + " available";
        if (userSkills > 0) catalogMeta += "  \xc2\xb7  " + std::to_string(userSkills) + " user";
        if (projectSkills > 0)
            catalogMeta += "  \xc2\xb7  " + std::to_string(projectSkills) + " project";
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextUnformatted(catalogMeta.c_str());
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * S));
        if (ui::kit::secondaryButton("New skill", true, nullptr, ImVec2(-1, 0))) {
            flushSkillEdits(st, /*force=*/true);
            ++s_ovSkills.bodyReq;  // drop any skill body still loading
            s_ovSkills.bodyInFlight = false;
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
        ImGui::Separator();

        if (!s_ovSkills.loaded && s_ovSkills.listInFlight) {
            ImGui::TextUnformatted("Loading skills\xe2\x80\xa6");
        } else if (s_ovSkills.skills.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                       ui::palette.textDim[2], 1));
            ImGui::TextWrapped("No skills found. Create a user skill to add reusable guidance.");
            ImGui::PopStyleColor();
        } else {
            const float lineH = ImGui::GetTextLineHeight();
            const float bodyH = lineH * 3.0f + 4.0f * S;
            for (size_t i = 0; i < s_ovSkills.skills.size(); ++i) {
                const auto& sk = s_ovSkills.skills[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovSkills.creating && s_ovSkills.selected == sk.name);
                ImVec2 textPos;
                float bodyW = 0.0f;
                const std::string rowLabel =
                    ui::foldToAtlasGlyphs(sk.name) + "##skill_catalog_row";
                if (settingsChildRowSelectable(rowLabel.c_str(), sel, bodyH, &textPos, &bodyW)) {
                    flushSkillEdits(st, /*force=*/true);  // never drop the previous skill's edits
                    s_ovSkills.creating = false;
                    s_ovSkills.selected = sk.name;
                    ovLoadSkillBody(st, sk.name);
                }
                if (sel) ImGui::SetItemDefaultFocus();

                const float textW = ImMax(40.0f * S, bodyW);
                const std::string description = sk.description.empty()
                    ? "No description provided."
                    : ui::foldToAtlasGlyphs(sk.description);
                const std::string descriptionFit = ellipsizeToFit(description, textW);
                std::string source = sk.source.empty() ? "builtin" : sk.source;
                std::string injection = sk.inject.empty() ? "manual" : sk.inject;
                const std::string metadata = ellipsizeToFit(ui::foldToAtlasGlyphs(
                    source + "  \xc2\xb7  " + injection + " injection"), textW);
                ImDrawList* dl = ImGui::GetWindowDrawList();
                dl->AddText(ImVec2(textPos.x, textPos.y + lineH + 2.0f * S),
                            ui::Palette::toU32(ui::palette.textDim),
                            descriptionFit.c_str());
                const float* metaColor = sk.source == "user"
                    ? ui::palette.accent
                    : (sk.source == "project" ? ui::palette.statusOk : ui::palette.textDim);
                dl->AddText(ImVec2(textPos.x, textPos.y + lineH * 2.0f + 4.0f * S),
                            ui::Palette::toU32(metaColor), metadata.c_str());
                if (ImGui::IsItemHovered()) {
                    ImGui::SetTooltip("%s\n%s\nSource: %s\nInjection: %s",
                                      ui::foldToAtlasGlyphs(sk.name).c_str(),
                                      description.c_str(), source.c_str(), injection.c_str());
                }
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 2.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##set_skill_ed", "Skill editor", ImVec2(0, colH))) {
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
            const OvSkillRow* selectedSkill = nullptr;
            for (const auto& sk : s_ovSkills.skills)
                if (sk.name == s_ovSkills.selected) { selectedSkill = &sk; break; }
            ui::InterwebzKeyValueRow("Skill", s_ovSkills.selected.c_str());
            ui::InterwebzKeyValueRow(
                "Source", selectedSkill && !selectedSkill->source.empty()
                    ? selectedSkill->source.c_str() : "builtin");
            ui::InterwebzKeyValueRow(
                "Injection", selectedSkill && !selectedSkill->inject.empty()
                    ? selectedSkill->inject.c_str() : "manual");
            ui::InterwebzKeyValueRow(
                "Status", s_ovSkills.bodyInFlight
                    ? "Loading" : (s_ovSkills.bodyDirty ? "Unsaved changes" : "Saved"));
        }

        if (s_ovSkills.creating || !s_ovSkills.selected.empty()) {
            if (s_ovSkills.bodyInFlight && !s_ovSkills.creating) {
                ImGui::TextUnformatted("Loading body\xe2\x80\xa6");
            } else {
                const float edH = ImMax(100.0f * S,
                    ImGui::GetContentRegionAvail().y - 56.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##set_skillbody", s_ovSkills.body.data(),
                                              s_ovSkills.body.size(), ImVec2(-1, edH))) {
                    s_ovSkills.bodyDirty = true;
                    ++s_ovSkills.editSeq;
                    s_ovSkills.lastEdit = ImGui::GetTime();
                }
                if (st.fonts.mono) ImGui::PopFont();
            }

            // Existing skills autosave (flushSkillEdits); only a NEW skill needs an explicit
            // Create because it must be named first.
            if (s_ovSkills.creating) {
                const bool busySave = s_ovSkills.saveInFlight;
                if (ui::kit::primaryButton("Create##set_sksave", !busySave, "Saving\xe2\x80\xa6",
                                           ImVec2(-1, 0))) {
                    // Normalize name from field; rewrite frontmatter name if needed.
                    std::string name = s_ovSkills.newName;
                    // strip spaces
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) { return std::isspace(c); }), name.end());
                    if (name.empty()) name = "my-skill";
                    s_ovSkills.selected = name;
                    s_ovSkills.bodyName = name;  // the buffer IS this skill's body
                    s_ovSkills.creating = false;
                    ovSaveSkill(st);
                }
            }

            // Delete only for user skills.
            if (!s_ovSkills.creating && !s_ovSkills.selected.empty()) {
                bool isUser = false;
                for (const auto& sk : s_ovSkills.skills)
                    if (sk.name == s_ovSkills.selected && sk.source == "user") {
                        isUser = true; break;
                    }
                if (isUser) {
                    if (ui::kit::dangerButton("Delete user skill", true, nullptr, ImVec2(-1, 0)))
                        ovDeleteSkill(st, s_ovSkills.selected);
                }
            }
        }
    }
    ui::InterwebzGroupEnd();
    ui::InterwebzTwoColumnEnd();
}

// Shorten `s` so it fits `maxW` px by trimming the STRING (never a clip rect), so a label
// can not lose its last character to a mis-tuned inset. UTF-8 boundary safe.
static std::string ovFitText(const std::string& s, float maxW) {
    static const char* kEll = "\xe2\x80\xa6";
    if (s.empty() || ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    const float ellW = ImGui::CalcTextSize(kEll).x;
    if (maxW <= ellW) return kEll;
    size_t n = s.size();
    while (n > 0) {
        --n;
        while (n > 0 && (static_cast<unsigned char>(s[n]) & 0xC0) == 0x80) --n;
        if (n == 0) break;
        if (ImGui::CalcTextSize(s.c_str(), s.c_str() + n).x + ellW <= maxW) break;
    }
    return s.substr(0, n) + kEll;
}

// Everything below renders text authored in Python (services/agent/advisor), so each helper folds
// its finished string through ui::foldToAtlasGlyphs — once, at the end, BEFORE the caller measures
// or clips it. Folding after ovFitText/CalcTextSize would clip against a width the paint never
// uses ("->" is wider than the notdef box it replaces).

// The rule's human title: `label` from triggers_list, falling back to the bare id so an override
// written before labels existed still gets a heading.
static std::string ovTriggerLabel(const OvTriggerRow& tr) {
    return ui::foldToAtlasGlyphs(tr.label.empty() ? tr.id : tr.label);
}

// Human reading of one keyword regex, for the Rules list: "\bbearing seat\b" -> "bearing seat",
// "(?i)\bcolou?rs?\b" -> "color", "\bbolt[\s-]?hole\b" -> "bolt hole", "(nut|washer)" ->
// "nut/washer". Anchors, inline flags and group syntax drop; an optional/repeated atom (x?, x*)
// drops unless it is a separator, which reads as one space; alternation reads as "/". The raw
// pattern stays in the row tooltip and the editor. Falls back to the pattern when nothing
// readable is left.
static std::string ovRegexWords(const std::string& re) {
    std::string out;
    std::vector<size_t> groups;   // out.size() at each open '('
    size_t atom = 0;              // out.size() before the most recent atom
    const size_t n = re.size();
    size_t i = 0;
    while (i < n) {
        const char c = re[i];
        if (c == '\\' && i + 1 < n) {
            const char e = re[i + 1];
            i += 2;
            if (e == 'b' || e == 'B' || e == 'A' || e == 'Z' || e == 'z') continue;
            atom = out.size();
            if (e == 's') out += ' ';
            else if (e == 'd') out += '#';
            else if (e != 'w' && e != 'W' && e != 'S' && e != 'D') out += e;
            continue;
        }
        if (c == '(') {
            ++i;
            if (i < n && re[i] == '?') {
                ++i;
                size_t j = i;
                while (j < n && std::isalpha((unsigned char)re[j])) ++j;
                if (j < n && re[j] == ')') { i = j + 1; continue; }   // inline flags "(?i)"
                while (i < n && (re[i] == ':' || re[i] == '=' || re[i] == '!' || re[i] == '<'))
                    ++i;   // non-capturing / lookaround opener
            }
            groups.push_back(out.size());
            continue;
        }
        if (c == ')') {
            atom = groups.empty() ? out.size() : groups.back();
            if (!groups.empty()) groups.pop_back();
            ++i;
            continue;
        }
        if (c == '[') {
            size_t j = i + 1;
            bool sep = false;
            char first = 0;
            while (j < n && re[j] != ']') {
                if (re[j] == '\\' && j + 1 < n) {
                    if (re[j + 1] == 's') sep = true;
                    else if (!first) first = re[j + 1];
                    j += 2;
                    continue;
                }
                if (re[j] == ' ' || re[j] == '_' || re[j] == '-') sep = true;
                else if (!first && re[j] != '^') first = re[j];
                ++j;
            }
            i = j < n ? j + 1 : n;
            atom = out.size();
            if (sep) out += ' ';
            else if (first) out += first;
            continue;
        }
        if (c == '?' || c == '*') {
            const bool sepAtom = atom < out.size() &&
                                 out.find_first_not_of(' ', atom) == std::string::npos;
            out.resize(ImMin(atom, out.size()));
            if (sepAtom) out += ' ';
            ++i;
            continue;
        }
        if (c == '{') {
            while (i < n && re[i] != '}') ++i;
            ++i;
            continue;
        }
        if (c == '+' || c == '^' || c == '$') { ++i; continue; }
        atom = out.size();
        out += (c == '|') ? '/' : (c == '.') ? ' ' : c;
        ++i;
    }
    // Collapse runs of spaces, trim, and tighten " / " to "/".
    std::string tidy;
    for (char ch : out) {
        if (ch == ' ' && (tidy.empty() || tidy.back() == ' ' || tidy.back() == '/')) continue;
        if (ch == '/' && !tidy.empty() && tidy.back() == ' ') tidy.pop_back();
        tidy += ch;
    }
    while (!tidy.empty() && (tidy.back() == ' ' || tidy.back() == '/')) tidy.pop_back();
    while (!tidy.empty() && tidy.front() == '/') tidy.erase(0, 1);
    return tidy.empty() ? re : tidy;
}

// Engine identifier as words ("build_failed" -> "build failed").
static std::string ovIdWords(std::string s) {
    std::replace(s.begin(), s.end(), '_', ' ');
    return s;
}

// One compact line naming what fires a rule in plain words: the trigger words its keyword
// patterns match, the tools and the events. Raw patterns live in the tooltip and the editor.
static std::string ovTriggerSummary(const OvTriggerRow& tr) {
    std::string s;
    auto add = [&s](const char* tag, const std::vector<std::string>& v,
                    std::string (*read)(const std::string&)) {
        std::vector<std::string> shown;
        for (const auto& e : v) {
            std::string w = read(e);
            if (std::find(shown.begin(), shown.end(), w) == shown.end()) shown.push_back(w);
        }
        if (shown.empty()) return;
        if (!s.empty()) s += "  \xc2\xb7  ";
        s += tag;
        for (size_t i = 0; i < shown.size(); ++i) s += (i ? ", " : " ") + shown[i];
    };
    add("Words:", tr.keywords, ovRegexWords);
    add("Tools:", tr.tools, [](const std::string& x) { return ovIdWords(x); });
    add("Events:", tr.events, [](const std::string& x) { return ovIdWords(x); });
    if (s.empty()) s = "No trigger \xe2\x80\x94 never fires";
    return ui::foldToAtlasGlyphs(s);
}

// The same trigger unclipped, plus the rate limits that explain a rule staying quiet.
static std::string ovTriggerTooltip(const OvTriggerRow& tr) {
    std::string t = tr.label.empty() ? tr.id : (tr.label + "  (" + tr.id + ")");
    if (!tr.purpose.empty()) t += "\n" + tr.purpose;
    auto block = [&t](const char* head, const std::vector<std::string>& v) {
        if (v.empty()) return;
        t += "\n\n";
        t += head;
        for (const auto& e : v) t += "\n  " + e;
    };
    std::vector<std::string> words;
    for (const auto& k : tr.keywords) words.push_back(ovRegexWords(k));
    block("Trigger words:", words);
    block("Patterns (regex):", tr.keywords);
    block("Tools:", tr.tools);
    block("Events:", tr.events);
    if (tr.keywords.empty() && tr.tools.empty() && tr.events.empty())
        t += "\n\nNo trigger \xe2\x80\x94 this rule can never fire.";
    if (!tr.pack.empty()) t += "\n\nInjects pack: " + tr.pack;
    t += "\n" + std::to_string(tr.budgetTokens) + "-token budget \xc2\xb7 "
       + std::to_string(tr.cooldownTurns) + "-turn cooldown";
    t += std::string("\nSource: ") + (tr.source.empty() ? "built-in" : tr.source.c_str());
    return ui::foldToAtlasGlyphs(t);
}

// Seed the editor with a blank, hand-edited rule + pack (the guided "New rule…" popup is the
// primary create path; this is the expert route from the editor's empty state).
static void ovBeginBlankRule() {
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
    std::strncpy(s_ovTrig.keywords, "\\bmy_keyword\\b", sizeof(s_ovTrig.keywords) - 1);
    std::memset(s_ovTrig.tools, 0, sizeof(s_ovTrig.tools));
    std::memset(s_ovTrig.events, 0, sizeof(s_ovTrig.events));
    std::memset(s_ovTrig.exemplar, 0, sizeof(s_ovTrig.exemplar));
    std::fill(s_ovTrig.packBody.begin(), s_ovTrig.packBody.end(), 0);
    std::strncpy(s_ovTrig.packBody.data(),
                 "# My context pack\n\nImportant considerations:\n- ...\n",
                 s_ovTrig.packBody.size() - 1);
    s_ovTrig.prerunCalcs = nlohmann::json::array();
    // No Python source behind these C++ templates: save() must write the buffers.
    for (OvRawField* f : {&s_ovTrig.purposeSrc, &s_ovTrig.packNameSrc, &s_ovTrig.exemplarSrc,
                          &s_ovTrig.keywordsSrc, &s_ovTrig.toolsSrc, &s_ovTrig.eventsSrc,
                          &s_ovTrig.packBodySrc})
        f->clear();
    s_ovTrig.bodyDirty = true;
    s_ovTrig.packDirty = true;
    std::memset(s_ovTrig.newId, 0, sizeof(s_ovTrig.newId));
    std::strncpy(s_ovTrig.newId, "my_trigger", sizeof(s_ovTrig.newId) - 1);
}

static void drawSettingsContextTab(AppState& st, float S) {
    ovPollTriggers(st, /*force=*/false);

    if (s_ovTrig.rpcMissing) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextWrapped(
            "triggers_* RPC not available \xe2\x80\x94 Rules editor hidden.");
        ImGui::PopStyleColor();
        return;
    }
    if (!s_ovTrig.lastError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
        ImGui::TextWrapped("%s", s_ovTrig.lastError.c_str());
        ImGui::PopStyleColor();
    }

    const ImVec4 dimCol(ui::palette.textDim[0], ui::palette.textDim[1],
                        ui::palette.textDim[2], 1);
    const float titleBudget = ImGui::GetTextLineHeight() + 10.0f * S;
    const float colH = ImMax(180.0f * S, ImGui::GetContentRegionAvail().y - titleBudget);
    ui::InterwebzTwoColumnBegin(10.0f);
    if (ui::InterwebzGroupBegin("##set_trig_list", "Rules", ImVec2(0, colH))) {
        int enabledRules = 0;
        int customRules = 0;
        for (const auto& tr : s_ovTrig.rows) {
            if (tr.enabled) ++enabledRules;
            if (tr.source == "override" || tr.source == "user") ++customRules;
        }
        std::string rulesMeta = std::to_string(s_ovTrig.rows.size()) + " available  \xc2\xb7  "
                              + std::to_string(enabledRules) + " enabled";
        if (customRules > 0)
            rulesMeta += "  \xc2\xb7  " + std::to_string(customRules) + " customized";
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextUnformatted(rulesMeta.c_str());
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * S));
        // ONE create entry: the guided authoring popup (rules, skills, context packs). The blank
        // hand-edited template is offered from the editor pane's empty state instead of as a
        // second, near-identical button beside this one.
        if (ui::kit::secondaryButton("New rule\xe2\x80\xa6", true, nullptr, ImVec2(-1, 0)))
            s_rulesAuthorOpen = true;
        ui::kit::itemTooltip("Guided authoring for a rule, skill or context pack.");
        ImGui::Separator();

        if (!s_ovTrig.loaded && s_ovTrig.listInFlight) {
            ImGui::TextUnformatted("Loading rules\xe2\x80\xa6");
        } else if (s_ovTrig.rows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, dimCol);
            ImGui::TextWrapped("No rules found. Create one to inject guidance when work matches.");
            ImGui::PopStyleColor();
        } else {
            const float lineH = ImGui::GetTextLineHeight();
            const float bodyH = lineH * 4.0f + 6.0f * S;
            for (size_t i = 0; i < s_ovTrig.rows.size(); ++i) {
                const auto& tr = s_ovTrig.rows[i];
                ImGui::PushID((int)i);
                const bool sel = (!s_ovTrig.creating && s_ovTrig.selected == tr.id);
                ImVec2 textPos;
                float bodyW = 0.0f;
                const std::string label = ovTriggerLabel(tr);
                const std::string rowLabel = label + "##rule_catalog_row";
                if (settingsChildRowSelectable(rowLabel.c_str(), sel, bodyH, &textPos, &bodyW)) {
                    flushTriggerEdits(st, /*force=*/true);  // never drop the previous rule's edits
                    s_ovTrig.creating = false;
                    s_ovTrig.selected = tr.id;
                    ovLoadTriggerBody(st, tr.id);
                }
                if (sel) ImGui::SetItemDefaultFocus();

                const float textW = ImMax(40.0f * S, bodyW);
                const char* source = tr.source == "override" ? "Customized"
                                   : tr.source == "user"     ? "Your rule"
                                                             : "Built-in";
                std::string meta = tr.enabled ? "Enabled" : "Disabled";
                meta += std::string("  \xc2\xb7  ") + source + "  \xc2\xb7  "
                      + std::to_string(tr.budgetTokens) + "-token budget  \xc2\xb7  "
                      + std::to_string(tr.cooldownTurns) + "-turn cooldown";
                const std::string metaFit = ovFitText(meta, textW);
                std::string purpose;
                if (!tr.purpose.empty())
                    purpose = ui::foldToAtlasGlyphs(tr.purpose);
                else if (!tr.pack.empty())
                    purpose = ui::foldToAtlasGlyphs("Injects pack " + tr.pack);
                else
                    purpose = "No purpose provided.";
                const std::string purposeFit = ovFitText(purpose, textW);
                const bool never = tr.keywords.empty() && tr.tools.empty() && tr.events.empty();
                const std::string matchFit = ovFitText(ovTriggerSummary(tr), textW);
                ImDrawList* dl = ImGui::GetWindowDrawList();
                dl->AddText(ImVec2(textPos.x, textPos.y + lineH + 2.0f * S),
                            ui::Palette::toU32(tr.enabled
                                ? ui::palette.textDim : ui::palette.statusWarn),
                            metaFit.c_str());
                dl->AddText(ImVec2(textPos.x, textPos.y + lineH * 2.0f + 4.0f * S),
                            ui::Palette::toU32(ui::palette.textDim), purposeFit.c_str());
                dl->AddText(ImVec2(textPos.x, textPos.y + lineH * 3.0f + 6.0f * S),
                            ui::Palette::toU32(never
                                ? ui::palette.statusWarn : ui::palette.text),
                            matchFit.c_str());
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", ovTriggerTooltip(tr).c_str());
                ImGui::PopID();
                ImGui::Dummy(ImVec2(0, 3.0f * S));
            }
        }
    }
    ui::InterwebzGroupEnd();

    ui::InterwebzTwoColumnNext();
    if (ui::InterwebzGroupBegin("##set_trig_ed", "Rule editor", ImVec2(0, colH))) {
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
            if (ui::kit::emptyState(nullptr, "No rule selected",
                                    "Pick a rule on the left to edit it, or start a blank rule "
                                    "and write its trigger and pack by hand.",
                                    "Start blank rule"))
                ovBeginBlankRule();
        } else {
            // Label first (Python-authored, so folded), id kept as its own row.
            const OvTriggerRow* selRow = nullptr;
            for (const auto& tr : s_ovTrig.rows)
                if (tr.id == s_ovTrig.selected) { selRow = &tr; break; }
            const std::string selLabel = selRow ? ovTriggerLabel(*selRow) : std::string();
            if (!selLabel.empty() && selLabel != s_ovTrig.selected)
                ui::InterwebzKeyValueRow("Rule", selLabel.c_str());
            ui::InterwebzKeyValueRow("Id", s_ovTrig.selected.c_str());
            ui::InterwebzKeyValueRow(
                "Source", selRow && !selRow->source.empty() ? selRow->source.c_str() : "builtin");
        }

        if (s_ovTrig.creating || !s_ovTrig.selected.empty()) {
            if (s_ovTrig.bodyInFlight && !s_ovTrig.creating) {
                ImGui::TextUnformatted("Loading\xe2\x80\xa6");
            } else {
                if (ui::kit::checkbox("trig_enabled", "Enabled", &s_ovTrig.enabled,
                                      "Off = never inject this pack (user override of "
                                      "shipped)."))
                    ovTrigEdited(/*immediate=*/true);

                ImGui::TextUnformatted("Purpose");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_purpose", s_ovTrig.purpose,
                                     sizeof(s_ovTrig.purpose)))
                    ovTrigEdited();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Why this pack exists — what important considerations it "
                        "forces the model to take.");

                ImGui::TextUnformatted("Keywords (regex, one per line)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputTextMultiline("##set_kws", s_ovTrig.keywords,
                        sizeof(s_ovTrig.keywords),
                        ImVec2(-1, 56.0f * S)))
                    ovTrigEdited();

                ImGui::TextUnformatted("Tools (names that fire this)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_tools", s_ovTrig.tools,
                                     sizeof(s_ovTrig.tools)))
                    ovTrigEdited();
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip(
                        "Comma or newline separated tool names, e.g. part_place, fit_calc.");

                ImGui::TextUnformatted("Events");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_events", s_ovTrig.events,
                                     sizeof(s_ovTrig.events)))
                    ovTrigEdited();

                ImGui::TextUnformatted("Context pack name");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_packname", s_ovTrig.packName,
                                     sizeof(s_ovTrig.packName)))
                    ovTrigEdited();

                ImGui::TextUnformatted("Exemplar (short worked example)");
                ImGui::SetNextItemWidth(-1);
                if (ImGui::InputText("##set_exemplar", s_ovTrig.exemplar,
                                     sizeof(s_ovTrig.exemplar)))
                    ovTrigEdited();

                {
                    ui::kit::FieldGroup limits("trig_limits", "Cooldown (turns)");
                    if (ui::kit::fieldInt("trig_budget", "Token budget", &s_ovTrig.budgetTokens,
                                          ui::kit::FieldOpts().withRange(50, 2000).withTip(
                                              "Most tokens this pack may add to one turn.")))
                        ovTrigEdited();
                    if (ui::kit::fieldInt("trig_cooldown", "Cooldown (turns)",
                                          &s_ovTrig.cooldownTurns,
                                          ui::kit::FieldOpts().withRange(0, 32).withTip(
                                              "Turns to wait before this rule may fire again.")))
                        ovTrigEdited();
                }

                ImGui::TextUnformatted("Pack body (markdown injected into context)");
                const float edH = ImMax(80.0f * S,
                    ImGui::GetContentRegionAvail().y - 72.0f * S);
                if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
                if (ImGui::InputTextMultiline("##set_packbody",
                        s_ovTrig.packBody.data(), s_ovTrig.packBody.size(),
                        ImVec2(-1, edH))) {
                    ovTrigEdited();
                    s_ovTrig.packDirty = true;
                }
                if (st.fonts.mono) ImGui::PopFont();
            }

            // Existing rules autosave (flushTriggerEdits); only a NEW rule needs an explicit
            // Create because it must be named first.
            if (s_ovTrig.creating) {
                const bool busySave = s_ovTrig.saveInFlight;
                if (ui::kit::primaryButton("Create##set_trsave", !busySave, "Saving\xe2\x80\xa6",
                                           ImVec2(-1, 0))) {
                    std::string name = s_ovTrig.newId;
                    name.erase(std::remove_if(name.begin(), name.end(),
                        [](unsigned char c) {
                            return !(std::isalnum(c) || c == '_' || c == '-');
                        }), name.end());
                    if (name.empty()) name = "my_trigger";
                    s_ovTrig.selected = name;
                    s_ovTrig.bodyId = name;  // the buffers ARE this rule's body
                    s_ovTrig.creating = false;
                    ovSaveTrigger(st);
                }
            } else {
                dimText(s_ovTrig.bodyDirty
                            ? "Saving changes\xe2\x80\xa6"
                            : "Autosaved. Edits write a user override (shipped triggers.yaml is "
                              "never rewritten).");
            }

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
                    if (ui::kit::dangerButton(isUserOnly ? "Delete user trigger"
                                                         : "Reset to shipped",
                                              true, nullptr, ImVec2(-1, 0)))
                        ovDeleteTriggerOverride(st, s_ovTrig.selected);
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
// Routes distinguish AI sections, so a search hit never inherits the last visited section.
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
    { "Box.com mirror",           "General",      1 },
    { "Mirror saved projects to Box", "General",  1 },
    { "Box OAuth",                "General",      1 },
    { "Sync now",                 "General",      1 },
    { "Engine routing",           "General",      1 },
    { "C++ core",                 "General",      1 },
    { "Performance profiler",     "General",      1 },
    { "Profiler",                 "General",      1 },
    // AI (tab 2) — harness (session knobs), roles, tools+maintenance
    { "Session knobs",            "AI", SettingsAiHarness },
    { "Harness",                  "AI", SettingsAiHarness },
    { "Tools",                    "AI", SettingsAiTools },
    { "Dev Preview",              "AI", SettingsAiDevPreview },
    { "Prompt assembly",          "AI", SettingsAiDevPreview },
    { "Auto-Learn",               "AI", SettingsAiHarness },
    { "Compaction",               "AI", SettingsAiHarness },
    { "Tool families",            "AI", SettingsAiTools },
    { "Bug reports",              "AI", SettingsAiTools },
    { "Send bug reports",         "AI", SettingsAiTools },
    { "Sketch 2D",                "AI", SettingsAiRoles },
    { "Model 3D",                 "AI", SettingsAiRoles },
    { "Model roles",              "AI", SettingsAiRoles },
    { "Hidden models",            "Providers", SettingsProviders },
    { "Harness organization",     "AI", SettingsAiHarness },
    { "Copilot mode",             "AI", SettingsAiHarness },
    { "Mode defaults",            "AI", SettingsAiHarness },
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
    // Generation block — the ONE Settings-modal home for these three controls, rendered by
    // drawAiSettingsSection(Generation) at the top of the Providers tab. "Reasoning effort"
    // used to target the AI tab, which has no such control anywhere: the search hit landed
    // the user on a page where the thing they searched for did not exist.
    { "Generation",               "Providers",    3 },
    { "Reasoning effort",         "Providers",    3 },
    { "Thinking effort",          "Providers",    3 },
    { "Max response tokens",      "Providers",    3 },
    { "Model context window",     "Providers",    3 },
    // AI: Benchmarks, Prompts, Skills, Rules
    { "Benchmarks",               "AI", SettingsAiBenchmarks },
    { "Agent benchmark",          "AI", SettingsAiBenchmarks },
    { "Artifact validity",        "AI", SettingsAiBenchmarks },
    { "Show legacy results",      "AI", SettingsAiBenchmarks },
    { "Model leaderboard",        "AI", SettingsAiBenchmarks },
    { "Run benchmark",            "AI", SettingsAiBenchmarks },
    { "Benchmark",                "AI", SettingsAiBenchmarks },
    { "Benchmark categories",     "AI", SettingsAiBenchmarks },
    { "Category run",             "AI", SettingsAiBenchmarks },
    { "Prompts",                  "AI", SettingsAiPrompts },
    { "Role prompts",             "AI", SettingsAiPrompts },
    { "Agent prompts",            "AI", SettingsAiPrompts },
    { "Prompt editor",            "AI", SettingsAiPrompts },
    { "System prompt",            "AI", SettingsAiPrompts },
    { "System prompt override",   "AI", SettingsAiPrompts },
    { "Skills",                   "AI", SettingsAiSkills },
    { "Skill editor",             "AI", SettingsAiSkills },
    { "Rules",                    "AI", SettingsAiRules },
    { "Rule triggers",            "AI", SettingsAiRules },
    { "Context injection",        "AI", SettingsAiRules },
    { "Triggers",                 "AI", SettingsAiRules },
    // Run behavior (lives on AI tab; was Agent tab 6)
    { "Plan preference",          "AI", SettingsAiHarness },
    { "Plan before building",     "AI", SettingsAiHarness },
    { "Plan mode",                "AI", SettingsAiHarness },
    { "Subagent preference",      "AI", SettingsAiHarness },
    { "Task list preference",     "AI", SettingsAiHarness },
    { "Role adoption",            "AI", SettingsAiHarness },
    { "Advisor preference",       "AI", SettingsAiHarness },
    { "Proactive advisor",        "AI", SettingsAiHarness },
    { "Project memory",           "AI", SettingsAiHarness },
    { "Memory budget",            "AI", SettingsAiHarness },
    { "Subagent tool-call budget", "AI", SettingsAiHarness },
    { "Tool call budget",         "AI", SettingsAiHarness },
    { "Subagent convergence warning", "AI", SettingsAiHarness },
    { "Run behavior",             "AI", SettingsAiHarness },
    { "Maintenance",              "AI", SettingsAiTools },
    { "Max subagent nesting",     "AI", SettingsAiHarness },
    // Vision routing is in Roles; parametric sequence is in Harness.
    { "Vision review model",      "AI", SettingsAiRoles },
    { "Vision routing",           "AI", SettingsAiRoles },
    { "Vision & spatial routing", "AI", SettingsAiRoles },
    { "Spatial model",            "AI", SettingsAiRoles },
    { "Seek-CAD split",           "AI", SettingsAiRoles },
    { "Parametric sequence mode", "AI", SettingsAiHarness },
    { "CAD sequence mode",        "AI", SettingsAiHarness },
    { "Feature sequence",         "AI", SettingsAiHarness },
    { "Monolithic solid",         "AI", SettingsAiHarness },
    { "Experimental",             "AI", SettingsAiHarness },
};
const int kSettingsEntryCount =
    (int)(sizeof(kSettingsEntries) / sizeof(kSettingsEntries[0]));

// ---- AI hub sub-section disclosure ----
// Which of the AI tab's 8 sub-sections are power surfaces.
//
// This split is measured, not guessed. A full inventory of the AI tab classified 155
// interactive controls as 10 EVERYDAY / 145 ADVANCED / 0 auto-derivable. Per-section:
//   Harness      9 everyday / 27 advanced
//   Roles        0 everyday / 59 advanced   <- per-role model pinning; the resolver default
//                                              is the normal path, and the everyday "which
//                                              model do I use" choice lives in Providers
//   Prompts      0 everyday /  8 advanced   <- authoring system/role instruction prose
//   Skills       0 everyday /  6 advanced
//   Rules        0 everyday / 16 advanced
//   Benchmarks   0 everyday /  5 advanced
//   Tools        1 everyday / 22 advanced   <- the 1 is "Repair / install runtime", which
//                                              Basic mode re-homes onto Harness (see the
//                                              Maintenance block in the Harness body) so
//                                              hiding this section costs nothing
//   Dev Preview  0 everyday /  2 advanced
// So Basic mode showing Harness alone is what the data supports, and it is the difference
// between ~10 controls and 155.
//
// Indices MUST stay aligned with kAiSubLabels in the AI tab; both are fixed 8-element
// arrays and the static_assert below pins the length.
const bool kAiSubAdvanced[8] = {
    false,  // 0 Harness      — session behavior; the only page with everyday controls
    true,   // 1 Roles        — per-role model/effort pinning
    true,   // 2 Prompts      — system + role prompt authoring
    true,   // 3 Skills       — skill catalog authoring
    true,   // 4 Rules        — context-injection trigger authoring
    true,   // 5 Benchmarks   — leaderboard + diagnostic runner
    true,   // 6 Tools        — tool-family gating, per-tool overrides, bug reports
    true,   // 7 Dev Preview  — dev-only prompt-assembly inspection
};
static_assert(sizeof(kAiSubAdvanced) / sizeof(kAiSubAdvanced[0]) == 8,
              "kAiSubAdvanced must carry one flag per kAiSubLabels entry");

// ---- Maintenance (runtime repair) ----
// Extracted so Basic mode can re-home it. The AI-tab inventory found this is the ONLY
// everyday control on the otherwise all-advanced Tools page, and a user whose runtime is
// broken must be able to repair it without first discovering the Advanced switch. Rendered
// on Tools when Advanced is on, on Harness when it is off — never both at once, so this is
// placement, not duplication.
static void drawRuntimeMaintenanceSection(AppState& st, float sc) {
    settingsGlassSectionBegin("##ai_maintenance", "Maintenance");
    dimText("Repair the local CAD engine runtime.");
    const bool canSetup = !st.workerBootPending && !st.setupRunning.load() && !st.anyRunActive();
    if (!canSetup) ImGui::BeginDisabled();
    if (ui::kit::secondaryButton("Repair / install runtime", true, nullptr, ImVec2(220.0f * sc, 0)))
        st.startSetup(true);
    if (!canSetup) ImGui::EndDisabled();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Rebuild the local Python/CAD runtime (same as "
                          "Chudware -> Repair / install runtime).");
    if (st.setupRunning.load()) {
        ImGui::SameLine();
        dimText("Running\xe2\x80\xa6");
    }
    settingsGlassSectionEnd();
}


namespace ui {

// settings_modal.h publishes drawHarnessSessionKnobs as ui::drawHarnessSessionKnobs (the AI
// workspace reuses the same Options-tab run-behavior grid), but the definition above lives in
// this file's anonymous namespace — which left the exported symbol undefined at link time.
// Forward to it. (:: is required: unqualified here would resolve to this wrapper.)
bool drawHarnessSessionKnobs(AppState& st, float sc) {
    return ::drawHarnessSessionKnobs(st, sc);
}

void flushSettingsModalEdits(AppState& st) {
    flushPromptEditors(st, /*force=*/true);
}

void flushSettingsModalRpcEdits(AppState& st) {
    if (!st.workerStarted) return;
    // Skills / Rules edits still inside their typing pause go out now...
    flushSkillRuleEdits(st, /*force=*/true);
    // ...and so does a model-assignment draft that never got an ack (the in-flight call is
    // already on the wire; a draft left behind by a failed/deferred save is re-sent once).
    if (!s_roles.pendingPatch.is_null() && !s_roles.rolesSaveInFlight && s_roles.rolesLoaded &&
        !s_roles.rolesRpcMissing)
        sendPendingModelRoles(st);
    // Wait (bounded) for those calls to be acknowledged: AsyncRpc::shutdown() aborts the one
    // in flight, which would lose the edit.
    if (s_ovSkills.saveInFlight || s_ovTrig.saveInFlight || s_roles.rolesSaveInFlight)
        st.rpc.drainUntilIdle(5000);
}

void drawQuickModelSizes(AppState& st, float captionColumn, unsigned int captionInk) {
    // The same poll/draft/ack/retry state and pickers as Settings → AI → Models; only the row
    // layout differs. Nothing to read or save until the agent service answers.
    const bool serviceReady = st.workerStarted && !st.workerBootPending && st.startupPhase.empty();
    if (serviceReady) pollModelRoles(st, /*force=*/false);
    ensureDefaultRoles();
    const bool ro = drawModelRolesStatus(st) || !serviceReady;
    if (!serviceReady) kit::dimText("Available once the agent service is ready.");
    drawModelSizeRows(st, ro, captionColumn, captionInk);
}

void beginExternalProviderSignIn(AppState& st, const std::string& provider) {
    ::beginOmpOAuth(st, provider, /*externalOwner=*/true);
}

ExternalProviderSignInStatus pollExternalProviderSignIn(AppState& st) {
    (void)st;   // one-call omp_login: progress arrives via streamed events, no poll RPC
    const OmpOAuthFlow& f = s_omp.oauth;
    ExternalProviderSignInStatus out;
    out.active = f.active;
    out.connected = f.status == "connected" || f.credentialSaved;
    out.browserOpened = f.browserOpened;
    out.provider = f.provider;
    out.status = f.status;
    out.progress = f.progress;
    out.instructions = f.instructions;
    out.url = !f.url.empty() ? f.url : f.launchUrl;
    out.error = !f.localError.empty() ? f.localError : f.error;
    return out;
}

void drawSettingsModal(AppState& st, bool busy) {
    Settings& settings = st.settings;
    // Debounced autosave of the Prompts-page editors runs here, not inside the Prompts page, so
    // switching tabs (or the page being scrolled off) cannot strand a pending edit.
    flushPromptEditors(st, /*force=*/false);
    flushSkillRuleEdits(st, /*force=*/false);

    // ---------- Settings window ----------
    static bool s_settingsOpen = false;
    // Every control in this dialog persists on change (theme / glass / units / roles / agent
    // knobs all save at their edit site, the system-prompt override debounce-flushes below) —
    // there is deliberately no Save/Cancel footer; closing the window never discards edits.
    bool opening = false;
    if (st.openSettings) {
        if (!s_settingsOpen) opening = true;
        s_settingsOpen = true;
        st.openSettings = false;
    }
    if (st.openSettingsTab >= 0 && !s_settingsOpen) { opening = true; s_settingsOpen = true; }
    // The main viewport does not enable ImGui keyboard navigation globally. Enable it while
    // Settings is open so combo rows and their search fields can be reached by keyboard too,
    // without changing navigation behavior in the rest of the application.
    static bool s_settingsEnabledKeyboardNav = false;
    if (opening && !(ImGui::GetIO().ConfigFlags & ImGuiConfigFlags_NavEnableKeyboard)) {
        ImGui::GetIO().ConfigFlags |= ImGuiConfigFlags_NavEnableKeyboard;
        s_settingsEnabledKeyboardNav = true;
    }
    // Workspace-folder edit buffer (General tab), reseeded from Settings on every open.
    static std::vector<char> s_wsBuf(512, 0);
    // Box.com mirror edit buffers (General tab), also reseeded on every open so Cancel-less
    // immediate persistence elsewhere can never leave stale secrets in the controls.
    static std::vector<char> s_boxFolderBuf(256, 0);
    static std::vector<char> s_boxDeveloperTokenBuf(1024, 0);
    static std::vector<char> s_boxClientIdBuf(512, 0);
    static std::vector<char> s_boxClientSecretBuf(1024, 0);
    static int s_boxAuthMethod = 0;
    // General-page text fields autosave when their edit ends (Enter / focus lost), when the page
    // changes, or when the modal closes. They count as dirty only after the user typed in them,
    // so a stale buffer can never overwrite a workspace folder changed elsewhere (opening a project).
    static bool s_wsDirty = false;
    static bool s_boxDirty = false;
    auto commitWorkspaceDir = [&]() {
        s_wsDirty = false;
        std::string chosen = s_wsBuf.data();
#ifdef __EMSCRIPTEN__
        // Never persist the literal '/workspace' stub — resolve the real root.
        if (chosen == "/workspace") chosen.clear();
#endif
        settings.workspaceDir = chosen;
        st.workspaceDir = chosen;  // empty: ensureWorkspaceDir re-resolves the default root
        saveSettings(settings);
        st.addLog("[ok] workspace folder set: " +
                  (chosen.empty() ? std::string("(default)") : chosen));
    };
    auto commitBoxSettings = [&](bool refreshStatus) {
        s_boxDirty = false;
        settings.boxFolderId =
            s_boxFolderBuf[0] ? std::string(s_boxFolderBuf.data()) : std::string("0");
        settings.boxAuthMethod = s_boxAuthMethod == 1 ? "oauth" : "developer_token";
        settings.boxDeveloperToken = s_boxDeveloperTokenBuf.data();
        settings.boxClientId = s_boxClientIdBuf.data();
        settings.boxClientSecret = s_boxClientSecretBuf.data();
        saveSettings(settings);
        if (refreshStatus) {
            s_boxMirrorUi.actionStatus = "Box settings saved";
            requestBoxStatus(st);
        }
    };
    if (opening) {
        openOmpProviderModal();
        std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
        std::strncpy(s_wsBuf.data(), settings.workspaceDir.c_str(), s_wsBuf.size() - 1);
        auto seedBoxBuffer = [](std::vector<char>& dst, const std::string& src) {
            std::fill(dst.begin(), dst.end(), 0);
            std::strncpy(dst.data(), src.c_str(), dst.size() - 1);
        };
        seedBoxBuffer(s_boxFolderBuf, settings.boxFolderId);
        seedBoxBuffer(s_boxDeveloperTokenBuf, settings.boxDeveloperToken);
        seedBoxBuffer(s_boxClientIdBuf, settings.boxClientId);
        seedBoxBuffer(s_boxClientSecretBuf, settings.boxClientSecret);
        s_boxAuthMethod = settings.boxAuthMethod == "oauth" ? 1 : 0;
        s_wsDirty = false;
        s_boxDirty = false;
        s_boxMirrorUi.actionStatus.clear();
        requestBoxStatus(st);
        // Make sure the glass engine reflects the persisted config before the Appearance
        // preview draws (idempotent — app startup wiring may already have pushed it).
        ui::setGlassConfig(settings.glassCfg);
    }
    float sc = ui::uiScale;
    ImGuiViewport* vp = ImGui::GetMainViewport();
    // The dialog lives inside the overlay band (below the ribbon, above the status bar) — the
    // same region every floating dialog anchors in. Sizing and centring against the whole
    // viewport work area parked the title strip over the ribbon's tab row. Before app.cpp has
    // published a band (first frame) the viewport work area is the fallback.
    const ui::OverlayBand& band = ui::overlayBand();
    const ImVec2 areaMin = band.valid ? ImVec2(band.minX, band.minY) : vp->WorkPos;
    const ImVec2 areaMax = band.valid ? ImVec2(band.maxX, band.maxY)
                                      : ImVec2(vp->WorkPos.x + vp->WorkSize.x,
                                               vp->WorkPos.y + vp->WorkSize.y);
    const ImVec2 areaSize(areaMax.x - areaMin.x, areaMax.y - areaMin.y);
    const float marginX = 28.0f * sc;
    const float marginY = 12.0f * sc;
    const float maxW = std::max(1.0f, areaSize.x - marginX * 2.0f);
    const float maxH = std::max(1.0f, areaSize.y - marginY * 2.0f);
    // Scale with the display instead of a flat 920x820: small screens keep the legacy size,
    // big ones get a wider modal — the Providers tab's two-column layouts only have room to
    // engage when the detail pane clears ~640*sc, which 920px never gives it. The band is a
    // hard cap: a floor larger than the band would push the dialog back over the chrome.
    const float winW = std::min(std::max(920.0f * sc, areaSize.x * 0.60f),
                                std::min(1280.0f * sc, maxW));
    const float winH = std::min(std::max(820.0f * sc, areaSize.y * 0.86f), maxH);
    if (s_settingsOpen) {
        // Begin() draws the title and background immediately. Moving the window AFTER Begin
        // moves only its cursor (ImGui explicitly warns this smears content): on a resized
        // browser viewport the title stayed put while the tabs jumped ~200px down. Clamp the
        // saved position before Begin so decoration, tab row and hit boxes share one origin.
        // The size tracks the band every frame; only correct position when it is outside it
        // so normal dragging still works.
        ImGui::SetNextWindowSize(ImVec2(winW, winH), ImGuiCond_Always);
        const ImVec2 center((areaMin.x + areaMax.x) * 0.5f,
                            (areaMin.y + areaMax.y) * 0.5f);
        if (opening) {
            ImGui::SetNextWindowPos(center, ImGuiCond_Always, ImVec2(0.5f, 0.5f));
        } else if (ImGuiWindow* saved = ImGui::FindWindowByName("Settings")) {
            const ImVec2 clamped(
                std::max(areaMin.x, std::min(saved->Pos.x, areaMax.x - winW)),
                std::max(areaMin.y, std::min(saved->Pos.y, areaMax.y - winH)));
            if (clamped.x != saved->Pos.x || clamped.y != saved->Pos.y)
                ImGui::SetNextWindowPos(clamped, ImGuiCond_Always);
        }
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
        // Rounded, softly elevated dialog: a large corner radius (the title strip follows it via
        // RoundCornersTop) and ONE low-alpha glass hairline as the outer edge. FrameBorderSize
        // stays zero — RenderFrame strokes the title rect with ImGuiCol_Border independently of
        // the window border, and that inner hairline under the title read as a hard seam.
        // WindowPadding is horizontally zero so each page body reaches the modal's right edge and
        // its pill scrollbar rides that edge; the nav rail insets itself below.
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(ui::palette.glassBorder[0], ui::palette.glassBorder[1],
                   ui::palette.glassBorder[2], ui::palette.glassBorder[3]));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, ui::metrics::rounding(12.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, ui::metrics::stroke(1.0f));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 0.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, ui::metrics::px(8.0f)));
        bool settingsBegun = ImGui::Begin("Settings", &s_settingsOpen,
                         ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoDocking);
        // The native title-bar X has no built-in ItemInfo label; expose its real hitbox to
        // pointer-driven UI scenarios after Begin has submitted the window decorations.
        {
            ImGuiContext& g = *GImGui;
            IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetCurrentWindow()->GetID("#CLOSE"),
                                        "##settings_close", 0);
        }
        // Decorations render inside Begin, so the chrome vars pop immediately after it —
        // widgets inside keep their normal frame borders.
        ImGui::PopStyleVar(3);    // FrameBorderSize, WindowBorderSize, WindowRounding
        ImGui::PopStyleColor(4);  // Border / TitleBg / TitleBgActive / TitleBgCollapsed
        // All placement is resolved above, before Begin paints the window decorations.
        if (settingsBegun) {
        // Keep a role deep link until its asynchronous catalog has arrived.
        bool jumpToPrompts = false;
        if (!st.pendingPromptRole.empty()) {
            s_pendingPromptSelection = std::move(st.pendingPromptRole);
            st.pendingPromptRole.clear();
            selectPendingPrompt();
            if (!s_promptsLoaded && st.workerStarted) loadPrompts(st);
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
        const bool explicitAiSection =
            jumpToTab >= SettingsAiHarness && jumpToTab <= SettingsAiDevPreview;
        if (explicitAiSection) {
            s_aiSubJump = jumpToTab - SettingsAiHarness;
            jumpToTab = SettingsAI;
        } else if (jumpToTab >= 4 && jumpToTab <= 7) {
            if (jumpToTab == 4) s_aiSubJump = 5;       // Benchmarks
            else if (jumpToTab == 5) s_aiSubJump = 2;  // Prompts
            else if (jumpToTab == 6) s_aiSubJump = 4;  // Rules (/rule in the composer)
            else if (jumpToTab == 7) s_aiSubJump = 1;  // Experimental -> Roles
            jumpToTab = SettingsAI;
        }
        if (jumpToPrompts) {
            s_aiSubJump = 2;
            jumpToTab = 2;
        }
        // A resolved jump drives the rail selection directly, so it lands the same frame.
        //
        // Reachability guarantee: a jump may target a section Basic mode hides — Settings
        // search indexes every entry, and the composer's /rule jumps straight at Rules,
        // which is an advanced section. Rather than dropping the user on a page that is not
        // in the rail, reveal advanced and persist it. This is why hiding a section can
        // never hide a capability: every programmatic route into one turns disclosure on.
        if (s_aiSubJump >= 0) {
            s_aiSubSel = s_aiSubJump;
            if (!st.settings.showAdvanced &&
                (explicitAiSection || (s_aiSubJump < 8 && kAiSubAdvanced[s_aiSubJump]))) {
                st.settings.showAdvanced = true;
                saveSettings(st.settings);
            }
        }
        // wantSettings (screenshot/automation shot) opens the AI tab.
        static bool s_wantSettingsTabDone = false;
        if (st.wantSettings && !s_wantSettingsTabDone) {
            s_wantSettingsTabDone = true;
            if (jumpToTab < 0) jumpToTab = 2;
        }

        // Pages: 0=Appearance 1=General 2=AI 3=Providers (indices are the openSettingsTab /
        // CHUDWARE_SHOT_SETTINGS_TAB contract — never reorder). The selection persists across
        // opens; a resolved jump drives it directly so it lands the same frame.
        static int s_settingsPage = 0;
        if (jumpToTab >= 0 && jumpToTab <= 3) s_settingsPage = jumpToTab;
        // Leaving General commits its typed fields (their deactivation may not have been drawn).
        if (s_settingsPage != SettingsGeneral) {
            if (s_wsDirty) commitWorkspaceDir();
            if (s_boxDirty) commitBoxSettings(/*refreshStatus=*/false);
        }

        // AI sub-section visibility, resolved BEFORE the nav rail: on wide dialogs the AI
        // sections are listed in the rail under "AI" (one navigation column, not a rail
        // inside a rail), and the AI page body below dispatches on the same stable indices.
        static const char* kAiSubLabels[8] = {
            "Harness", "Roles", "Prompts", "Skills",
            "Rules", "Benchmarks", "Tools", "Dev Preview"};
        // Basic mode paints only the everyday sections (kAiSubAdvanced, file scope). visIdx
        // maps a rail/combo row back to its REAL section index.
        const char* visLabels[8];
        int visIdx[8];
        int visN = 0;
        for (int i = 0; i < 8; ++i) {
            if (!st.settings.showAdvanced && kAiSubAdvanced[i]) continue;
            visLabels[visN] = kAiSubLabels[i];
            visIdx[visN] = i;
            ++visN;
        }
        // Turning Advanced off while parked on an advanced section would otherwise render a
        // body with no corresponding nav row. Fall back to Harness.
        if (!st.settings.showAdvanced && s_aiSubSel >= 0 && s_aiSubSel < 8 &&
            kAiSubAdvanced[s_aiSubSel])
            s_aiSubSel = 0;
        // With one visible section there is nothing to navigate (the Basic-mode shape).
        const bool aiNav = visN > 1;

        // Fixed navigation + scrolling page body. The outer container is non-scrolling and
        // TRANSPARENT — a glass frame around the whole region read as a nested border box; the
        // section cards carry their own glass. Each page wraps its content in
        // settingsTabBodyBegin/End so only the body scrolls.
        ImGui::BeginChild("##settabsarea", ImVec2(0, 0),
                          ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
        // Phone-width dialogs cannot afford a sidebar: the pages become one segmented row with
        // short labels instead, and the AI page keeps its own section picker.
        const bool compactTabs = ImGui::GetContentRegionAvail().x < 520.0f * sc;
        const bool navRail = !compactTabs;
        static const char* kPageLabel[4] = {"Appearance", "General", "AI", "Providers"};
        static const char* kPageShort[4] = {"Look", "General", "AI", "Keys"};
        static const char* kPageIcon[4]  = {"ws_appearance", "settings", "ai_panel", "eng_firm"};
        static const char* kPageId[4]    = {"nav_appearance", "nav_general", "nav_ai",
                                            "nav_providers"};
        const char* kAdvancedTip =
            "Advanced settings\n"
            "Off: only the controls most users need.\n"
            "On: every tuning, authoring and diagnostic control.\n\n"
            "Nothing is disabled while this is off — hidden controls keep their current "
            "values, config.json stays authoritative, and searching for a hidden setting "
            "turns this on for you.";
        // Basic / Advanced disclosure. Lives in the navigation (visible from every page) so it
        // reads as a property of the whole dialog rather than of one page. Why it exists: ~130
        // indexed entries plus an 8-section AI hub with no triage — Basic hides power controls
        // and disables nothing.
        auto advancedToggle = [&]() {
            bool adv = st.settings.showAdvanced;
            if (ui::kit::checkbox("set_advanced", "Advanced", &adv, kAdvancedTip)) {
                st.settings.showAdvanced = adv;
                saveSettings(st.settings);
            }
        };
        if (navRail) {
            const ImVec2 railMn = ImGui::GetCursorScreenPos();
            const float railAvailH = ImGui::GetContentRegionAvail().y;
            const float railW = ui::metrics::px(176.0f);
            ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                ImVec2(ui::metrics::px(8.0f), ui::metrics::px(4.0f)));
            ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                ImVec2(ui::metrics::px(6.0f), ui::metrics::px(2.0f)));
            // Short overlay bands must still let users reach every row and Advanced.
            ImGui::BeginChild("##settings_nav", ImVec2(railW, 0),
                              ImGuiChildFlags_AlwaysUseWindowPadding);
            // Rounded kit rows: frosted selection pill, animated hover wash, icon in accent
            // while selected — the same row every selected list in the app uses.
            ui::kit::pushDensity(ui::kit::Density::Compact);
            for (int i = 0; i < 4; ++i) {
                ui::kit::RowStyle rs;
                rs.icon     = kPageIcon[i];
                rs.iconTone = (s_settingsPage == i) ? ui::kit::Tone::Accent
                                                    : ui::kit::Tone::Neutral;
                rs.selected = (s_settingsPage == i);
                if (i == 3 && s_omp.oauth.active) {
                    rs.meta    = "sign-in";
                    rs.tooltip = "A provider sign-in is in progress";
                }
                if (ui::kit::row(kPageId[i], kPageLabel[i], rs)) s_settingsPage = i;
                // AI sections nest under their page while it is open.
                if (i == 2 && s_settingsPage == 2 && aiNav) {
                    ImGui::Indent(ui::metrics::px(22.0f));
                    for (int k = 0; k < visN; ++k) {
                        ImGui::PushID(visIdx[k]);
                        ui::kit::RowStyle sr;
                        sr.selected = (visIdx[k] == s_aiSubSel);
                        if (ui::kit::row("nav_ai_sub", visLabels[k], sr)) s_aiSubSel = visIdx[k];
                        ImGui::PopID();
                    }
                    ImGui::Unindent(ui::metrics::px(22.0f));
                }
            }
            ui::kit::popDensity();
            // Advanced switch pinned to the rail's foot (or right after the rows when the rail
            // is short), with the "more exists" cue above it so hidden sections are never a
            // surprise.
            const float lineH = ImGui::GetTextLineHeightWithSpacing();
            const float footH = ImGui::GetFrameHeight() +
                                (!st.settings.showAdvanced ? lineH : 0.0f);
            const float footY = ImGui::GetWindowHeight() - ImGui::GetStyle().WindowPadding.y -
                                footH;
            if (footY > ImGui::GetCursorPosY()) ImGui::SetCursorPosY(footY);
            if (!st.settings.showAdvanced && visN < 8) {
                char more[32];
                std::snprintf(more, sizeof(more), "+%d in Advanced", 8 - visN);
                dimText(more);
            }
            advancedToggle();
            ImGui::EndChild();
            ImGui::PopStyleVar(2);  // ItemSpacing, WindowPadding
            {
                float edge[4];
                ui::Palette::withAlpha(ui::palette.glassBorder, edge, 0.6f);
                const float dx = railMn.x + railW + ui::metrics::px(3.5f);
                ImGui::GetWindowDrawList()->AddLine(
                    ImVec2(dx, railMn.y), ImVec2(dx, railMn.y + railAvailH),
                    ui::Palette::toU32(edge), ui::metrics::stroke(1.0f));
            }
            ImGui::SameLine(0, ui::metrics::px(8.0f));
        } else {
            ImGui::Indent(ui::metrics::px(10.0f));
            ui::kit::segmented("##set_pages", kPageShort, 4, &s_settingsPage,
                               ImGui::GetContentRegionAvail().x - ui::metrics::px(10.0f),
                               kPageLabel);
            advancedToggle();
            ImGui::Unindent(ui::metrics::px(10.0f));
        }
        bool providerTabDrawn = false;
        {
            // ---- Appearance page (FIRST): theme + colored liquid glass (v2.1) ----
            // The canonical theme control lives here (moved from General; the View-menu item
            // stays a synced shortcut). The glass knobs edit Settings::glassCfg per group and
            // live-apply via ui::setGlassConfig + persist via saveSettings on every change —
            // so the persisted "appearance.glass" block is no longer a dead round-trip.
            if (s_settingsPage == 0) {
                settingsTabBodyBegin("##tab_appearance");
                settingsGlassSectionBegin("##app_theme", "Theme", 0.0f, /*first=*/true);
                bool isLight = (ui::activeTheme == ui::Theme::Light);
                if (ui::kit::checkbox("app_light", "Light theme", &isLight))
                    applyThemeNow(st, isLight);
                dimText("Switches the whole UI between the dark and light palettes. "
                        "Saved immediately.");
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##app_accent", "Accent colors");
                dimText("Primary is the color a modeling operation's result is drawn in \xe2\x80\x94 "
                        "the same role the 3D ribbon icons bake their new geometry with. Secondary "
                        "is the direction cue: arrows, axes, annotation. The ribbon retints as you "
                        "drag. Saved immediately.");
                ImGui::Dummy(ImVec2(0, 4 * sc));
                {
                    bool accentChanged = false;
                    // Same aligned label column as the glass rows below.
                    const float accLabelW = 150.0f * sc;
                    auto accentRow = [&](const char* label) {
                        ImGui::AlignTextToFramePadding();
                        ImGui::TextUnformatted(label);
                        ImGui::SameLine(accLabelW);
                    };
                    accentRow("Primary");
                    if (ImGui::ColorEdit3("##accentprimary", settings.accentPrimary,
                                          ImGuiColorEditFlags_NoInputs))
                        accentChanged = true;
                    ImGui::SameLine();
                    dimText("(extrudes, bosses \xe2\x80\x94 what the operation produced)");
                    accentRow("Secondary");
                    if (ImGui::ColorEdit3("##accentsecondary", settings.accentSecondary,
                                          ImGuiColorEditFlags_NoInputs))
                        accentChanged = true;
                    ImGui::SameLine();
                    dimText("(direction arrows, axes, dimensions)");
                    ImGui::Dummy(ImVec2(0, 4 * sc));
                    // "##accent" only disambiguates the ImGui id: the Liquid Glass section below
                    // has a button with the identical visible label, and two same-label buttons in
                    // one window share an id (the second one becomes unclickable).
                    if (ui::kit::secondaryButton("Reset to defaults##accent", true, nullptr, ImVec2(150.0f * sc, 28.0f * sc))) {
                        std::memcpy(settings.accentPrimary, ui::kDefaultAccentPrimary,
                                    sizeof(settings.accentPrimary));
                        std::memcpy(settings.accentSecondary, ui::kDefaultAccentSecondary,
                                    sizeof(settings.accentSecondary));
                        accentChanged = true;
                    }
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Restore the shipped orange / blue pair");
                    if (accentChanged) {
                        // Live-apply then persist, matching the glass pair below: setAccentColors
                        // rewrites the palette, the accent-derived ImGuiCol slots and the baked
                        // icon atlas in one call, so the whole UI tracks the drag.
                        ui::setAccentColors(settings.accentPrimary, settings.accentSecondary);
                        saveSettings(settings);
                    }
                }
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##app_brand", "Brand mark");
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
                        if (ImGui::IsItemVisible()) ui::anim::requestFramesAt(30.0f);
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
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##app_glass", "Liquid Glass");
                dimText("Per-surface styling for the glass panels floating over the viewport. "
                        "Dynamic color auto-tints each panel for max contrast against the live "
                        "model; pin a tint below to override it. Saved immediately.");
                ImGui::Dummy(ImVec2(0, 4 * sc));

                // Group selector — segmented pills. Global is the inheritance base: the other
                // groups fall back to it for any knob left at its default ("Reset" state).
                static int s_glassGroup = 0;
                static const char* kGroupLabel[(size_t)ui::GlassGroup::Count] = {
                    "Global", "Feature Manager", "Copilot", "Viewport HUD"};
                ui::kit::segmented("##glass_group", kGroupLabel, (int)ui::GlassGroup::Count,
                                   &s_glassGroup);

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
                    if (settingsPopupRowCombo("##glasslevel", &lv, kLevels, 5)) {
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
                if (ui::kit::checkbox("glass_dynamic",
                                      "Dynamic color \xe2\x80\x94 auto max-contrast vs model", &dyn)) {
                    g.dynamicColor = dyn ? 1 : 0;
                    glassChanged = true;
                }
                dimText("On (default): each panel samples the live viewport and steers its tint "
                        "for legibility. Off: the pinned tint above is used as-is.");

                ImGui::Dummy(ImVec2(0, 4 * sc));
                if (ui::kit::secondaryButton("Reset to defaults", true, nullptr, ImVec2(150.0f * sc, 28.0f * sc))) {
                    g = ui::GlassStyle();  // all sentinels -> inherit Global / built-in defaults
                    glassChanged = true;
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("Restore this group to inherited (Global / built-in) values");

                if (glassChanged) {
                    ui::setGlassConfig(settings.glassCfg);  // live-apply (idempotent)
                    saveSettings(settings);                 // glass knobs persist immediately
                }
                settingsGlassSectionEnd();

                // Live preview through the ACTUAL glass pipeline (adaptivePanel + glassPanel):
                // over the viewport it frosts the real backdrop with this group's resolved style;
                // over chrome it falls back to the layered glass. One stable id per group (the
                // adaptive cache requires the same group every frame for a given id).
                settingsGlassSectionBegin("##app_preview", "Preview");
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
                settingsGlassSectionEnd();
                settingsTabBodyEnd();
            }

            // ---- General page: Document (units) + System (engine readout, profiler) ----
            if (s_settingsPage == 1) {
                settingsTabBodyBegin("##tab_general");
                settingsGlassSectionBegin("##gen_document", "Document");
                static const char* kUnitHelp =
                    "The unit system for dimension display and input. The model is always "
                    "stored in canonical millimeters; this only changes how lengths are "
                    "shown and how a bare typed number is read. Angles stay in degrees in "
                    "both systems. You can also click the MMGS/IPS badge in the status bar "
                    "to switch.";
                static const char* kDisplayQualityHelp =
                    "How finely curved geometry is tessellated for DISPLAY. Fine (default) "
                    "keeps tubes, wheels and fillets smooth on large imported parts; Draft "
                    "trades smoothness for triangles on very heavy models. Geometry, mass "
                    "properties and exports are unaffected. Saved immediately and applied "
                    "to every open document.";
                const float docLabelW =
                    ImMax(settingsLabelLeadWidth("Units", true),
                          settingsLabelLeadWidth("Display quality", true));
                settingsLabelLead("Units", kUnitHelp, docLabelW);
                static const char* kUnitLabels[] = {"MMGS", "IPS"};
                static const char* kUnitTips[] = {"Millimeter, gram, second",
                                                  "Inch, pound, second"};
                int unitSel = st.ips() ? 1 : 0;
                if (ui::kit::segmented("##units", kUnitLabels, 2, &unitSel, 0.0f, kUnitTips)) {
                    settings.units = (unitSel == 1) ? Units::IPS : Units::MMGS;
                    // Saved immediately so the choice survives a Cancel (it is a display/document
                    // preference, like theme — and the status-bar toggle persists the same way).
                    saveSettings(settings);
                    st.addLog(std::string("[units] set to ") + (st.ips() ? "IPS" : "MMGS"));
                }
                ImGui::SameLine();
                ImGui::AlignTextToFramePadding();
                dimText(st.ips() ? "inch, pound, second" : "millimeter, gram, second");

                // Display tessellation quality (contracts/doc_ops.md §7). Saved immediately like
                // Units above, then pushed to every open part/assembly doc so the viewport
                // re-meshes at the new deflections without a reopen.
                settingsLabelLead("Display quality", kDisplayQualityHelp, docLabelW);
                static const char* kDisplayQualityLabels[] = {"Draft", "Standard", "Fine"};
                static const char* kDisplayQualityValues[] = {"draft", "standard", "fine"};
                int dqSel = 2;
                for (int i = 0; i < 3; ++i)
                    if (settings.displayQuality == kDisplayQualityValues[i]) dqSel = i;
                if (ui::kit::segmented("##display_quality", kDisplayQualityLabels, 3, &dqSel)) {
                    settings.displayQuality = kDisplayQualityValues[dqSel];
                    saveSettings(settings);
                    // AppState::syncDisplayQuality() pushes it to every open doc next frame; the
                    // same path restores the saved tier for docs opened after a restart.
                    st.addLog(std::string("[display] quality set to ") + settings.displayQuality);
                }
                dimText("Display-only mesh smoothness; lengths display/parse in the chosen units "
                        "everywhere. Both save immediately.");
                settingsGlassSectionEnd();

                // Configurations moved here from the left-pane strip (Graph took that slot).
                // Both embedded bodies paint their own "Configurations (n)" heading, so the
                // section gets only the section gap — a header here printed the title twice.
                ui::kit::pushDensity(ui::kit::Density::Cozy);
                ui::kit::gapSection();
                ui::kit::popDensity();
                if (st.configPanel) {
                    st.configPanel->drawEmbedded(st);
                } else {
                    ui::drawConfigurationsBody(st);
                }

                // Workspace folder (audit P1): workspace_dir was persisted but had NO UI —
                // users had to hand-edit config.json to re-root the File Explorer.
                settingsGlassSectionBegin("##gen_workspace", "Workspace");
                ImGui::AlignTextToFramePadding();
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
                    ImGui::SetNextItemWidth(-100.0f * sc);
                    if (ImGui::InputTextWithHint("##wsdir", s_wsHintWeb.c_str(),
                                                 s_wsBuf.data(), s_wsBuf.size()))
                        s_wsDirty = true;
                    if (ImGui::IsItemDeactivatedAfterEdit()) commitWorkspaceDir();
                    ImGui::SameLine();
                    if (ui::kit::secondaryButton("Browse\xe2\x80\xa6", true, nullptr, ImVec2(90.0f * sc, 0))) {
                        std::string chosen = openFolderDialog();
                        if (!chosen.empty() && chosen != "/workspace") {
                            std::fill(s_wsBuf.begin(), s_wsBuf.end(), 0);
                            std::strncpy(s_wsBuf.data(), chosen.c_str(), s_wsBuf.size() - 1);
                            commitWorkspaceDir();  // picking a folder is the commit
                        }
                    }
                    dimText("Root of the File Explorer on the server workspace (saved automatically).");
                }
#else
                // Truthful default label: defaultWorkspaceDir() applies the alias rule
                // (existing Documents/Chudware wins, else Documents/Chudware).
                static const std::string s_wsDefault = defaultWorkspaceDir();
                static const std::string s_wsHelp =
                    "The File Explorer's root folder and the default save location. "
                    "Leave empty to use " + s_wsDefault + ". Saved automatically when you "
                    "press Enter or leave the field.";
                static const std::string s_wsHint = "(default: " + s_wsDefault + ")";
                helpMarker(s_wsHelp.c_str());
                ImGui::SetNextItemWidth(-1.0f);
                if (ImGui::InputTextWithHint("##wsdir", s_wsHint.c_str(),
                                             s_wsBuf.data(), s_wsBuf.size()))
                    s_wsDirty = true;
                if (ImGui::IsItemDeactivatedAfterEdit()) commitWorkspaceDir();
                dimText("Root of the File Explorer and default save location (saved automatically).");
#endif
                settingsGlassSectionEnd();

                settingsGlassSectionBegin("##gen_box", "Box.com mirror");
                if (ui::kit::checkbox("box_enabled", "Mirror saved projects to Box",
                                      &settings.boxEnabled)) {
                    saveSettings(settings);
                    requestBoxStatus(st);
                }
                dimText("Saved project files are queued in the background; saves never wait for Box.");

                const float boxLabelW = ImMax(settingsLabelLeadWidth("Developer token", false),
                                              settingsLabelLeadWidth("Client secret", false));
                settingsLabelLead("Folder ID", nullptr, boxLabelW);
                ImGui::SetNextItemWidth(-1.0f);
                if (ImGui::InputTextWithHint("##box_folder_id", "Box folder id (0 = All Files)",
                                             s_boxFolderBuf.data(), s_boxFolderBuf.size()))
                    s_boxDirty = true;
                if (ImGui::IsItemDeactivatedAfterEdit()) commitBoxSettings(true);

                settingsLabelLead("Auth method", nullptr, boxLabelW);
                static const char* kBoxAuthLabels[] = {"Developer token", "OAuth"};
                if (ui::kit::segmented("##box_auth_method", kBoxAuthLabels, 2, &s_boxAuthMethod))
                    commitBoxSettings(true);  // the method gates "Connect Box account": apply at once

                settingsLabelLead("Developer token", nullptr, boxLabelW);
                ImGui::SetNextItemWidth(-1.0f);
                if (ImGui::InputTextWithHint("##box_developer_token", "Box developer token",
                                             s_boxDeveloperTokenBuf.data(),
                                             s_boxDeveloperTokenBuf.size(),
                                             ImGuiInputTextFlags_Password))
                    s_boxDirty = true;
                if (ImGui::IsItemDeactivatedAfterEdit()) commitBoxSettings(true);

                settingsLabelLead("Client ID", nullptr, boxLabelW);
                ImGui::SetNextItemWidth(-1.0f);
                if (ImGui::InputTextWithHint("##box_client_id", "Box OAuth client ID",
                                             s_boxClientIdBuf.data(), s_boxClientIdBuf.size()))
                    s_boxDirty = true;
                if (ImGui::IsItemDeactivatedAfterEdit()) commitBoxSettings(true);

                settingsLabelLead("Client secret", nullptr, boxLabelW);
                ImGui::SetNextItemWidth(-1.0f);
                if (ImGui::InputTextWithHint("##box_client_secret", "Box OAuth client secret",
                                             s_boxClientSecretBuf.data(),
                                             s_boxClientSecretBuf.size(),
                                             ImGuiInputTextFlags_Password))
                    s_boxDirty = true;
                if (ImGui::IsItemDeactivatedAfterEdit()) commitBoxSettings(true);


                const bool oauthLoginDisabled =
                    settings.boxAuthMethod != "oauth" ||
                    s_boxMirrorUi.connectInFlight || !st.workerStarted;
                ImGui::BeginDisabled(oauthLoginDisabled);
                if (ui::kit::secondaryButton(s_boxMirrorUi.connectInFlight
                                      ? "Connecting Box account\xe2\x80\xa6"
                                      : "Connect Box account\xe2\x80\xa6"))
                    beginBoxOAuthLogin(st);
                ImGui::EndDisabled();
                if (settings.boxAuthMethod != "oauth" &&
                    ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Select the OAuth auth method first");

                ImGui::SameLine();
                const bool syncDisabled =
                    s_boxMirrorUi.syncInFlight || !st.workerStarted || !settings.boxEnabled;
                ImGui::BeginDisabled(syncDisabled);
                if (ui::kit::secondaryButton(s_boxMirrorUi.syncInFlight ? "Syncing\xe2\x80\xa6" : "Sync now")) {
                    const std::string projectRoot = st.plmProjectPath();
                    if (projectRoot.empty())
                        s_boxMirrorUi.actionStatus =
                            "Box sync needs a current project or workspace folder";
                    else
                        beginBoxSync(st, projectRoot);
                }
                ImGui::EndDisabled();

                std::string boxStatusLine = s_boxMirrorUi.accountStatus;
                if (s_boxMirrorUi.statusInFlight) boxStatusLine = "Checking Box status\xe2\x80\xa6";
                if (!s_boxMirrorUi.actionStatus.empty())
                    boxStatusLine = s_boxMirrorUi.actionStatus + "  |  " + boxStatusLine;
                dimText(boxStatusLine.c_str());
                settingsGlassSectionEnd();

                // Theme moved to the Appearance page (the canonical control); engine routing +
                // profiler are status/launchers, folded under one System section (audit P2).
                settingsGlassSectionBegin("##gen_system", "System");
                // Routing is unconditional — the C++ core IS the engine
                // (the CHUDWARE_CPP_* escape hatches were removed with the Python oracle).
                if (core::cpp_core().available()) {
                    ImGui::TextUnformatted("Active engine: C++ core (libchudware)");
                    helpMarker("The in-process C++ core is the only modeling engine — documents, "
                               "sketching, FEA/CFD, CAM, ECAD, firmware and sourcing all run on "
                               "it. Services-side Python handles orchestration surfaces (agent "
                               "runs, parts catalog, reverse engineering, vendor sign-in, missions).");
                    dimText(("C++ core: libchudware loaded (ABI " +
                             core::cpp_core().abiVersion() + ")").c_str());
                    ImGui::BulletText("Modeling / documents / sketch: C++ core");
                    ImGui::BulletText("Simulation (FEA + CFD) / CAM / ECAD / firmware: C++ core");
                } else {
                    ImGui::TextUnformatted("Active engine: UNAVAILABLE (libchudware not loaded)");
                    // The path to name here is per-platform: the build tree carries the
                    // host's suffix (build.sh's PLATFORM_SUFFIX) and the engine takes that
                    // platform's shared-library extension. Naming the Linux pair on a Mac
                    // sends the user looking for a file that cannot exist.
#if defined(_WIN32)
                    static constexpr const char* kBuiltEngine =
                        "build/windows-occt/bin/libchudware.dll";
#elif defined(__APPLE__) && defined(__aarch64__)
                    static constexpr const char* kBuiltEngine =
                        "core/build-occt-macos-arm64/bin/libchudware.dylib";
#elif defined(__APPLE__)
                    static constexpr const char* kBuiltEngine =
                        "core/build-occt-macos-x86_64/bin/libchudware.dylib";
#else
                    static constexpr const char* kBuiltEngine =
                        "core/build-occt-linux/bin/libchudware.so";
#endif
                    // Composed once, not per frame: this branch redraws every frame the
                    // engine is missing.
                    static const std::string kEngineHint =
                        std::string("The C++ core (libchudware) is NOT loaded, and it is the only "
                                    "engine (the Python oracle was removed). Set the "
                                    "CHUDWARE_LIBCHUDWARE environment variable to the built "
                                    "library (") + kBuiltEngine +
                        ") or stage it next to the executable, then restart.";
                    helpMarker(kEngineHint.c_str());
                    dimText("C++ core: not loaded");
                }

                ImGui::Dummy(ImVec2(0, 8 * sc));
                dimText("Sampling profiler \xe2\x80\x94 see where modeling time goes (extrude / "
                        "fillet / boolean, into the kernel) as a live flame graph.");
                if (ui::kit::secondaryButton("Open Profiler", true, nullptr, ImVec2(160.0f * sc, 30.0f * sc)))
                    st.showProfilerWindow = true;
                settingsGlassSectionEnd();
                ImGui::Dummy(ImVec2(0, 6 * sc));
                dimText(("All settings here are stored in " + configPath() + ".").c_str());
                settingsTabBodyEnd();
            }
            // ---- AI page: unified hub (absorbed Benchmarks / Prompts / Agent / Experimental) ----
            // s_aiSubJump is resolved above, before the navigation, so a jump lands on the right
            // section the same frame.
            if (s_settingsPage == 2) {
                settingsTabBodyBegin("##tab_ai", /*scroll=*/false,
                                     /*padX=*/0.0f, /*padY=*/0.0f);

                // AI sub-navigation: wide dialogs list the sections in the settings rail under
                // "AI" (above); phone-width dialogs have no rail, so a section picker leads the
                // page. Only the SELECTED section's body runs each frame — Benchmarks' status
                // polling relies on that.
                if (aiNav && !navRail) {
                    int visSel = 0;
                    for (int k = 0; k < visN; ++k)
                        if (visIdx[k] == s_aiSubSel) {
                            visSel = k;
                            break;
                        }
                    ImGui::SetNextItemWidth(-1);
                    if (settingsPopupRowCombo("##ai_sub_pick", &visSel, visLabels, visN))
                        s_aiSubSel = visIdx[visSel];
                }
                ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                                    ImVec2(10.0f * sc, 6.0f * sc));
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

                    // Basic mode draws no rail (one visible section), so the "more exists"
                    // cue has to live in the body or the tab silently looks like it lost
                    // seven pages. Names them explicitly rather than just counting.
                    if (!aiNav && !st.settings.showAdvanced) {
                        dimText("Roles, Prompts, Skills, Rules, Benchmarks, Tools and Dev "
                                "Preview appear when Advanced is turned on.");
                        ImGui::Dummy(ImVec2(0, 6.0f * sc));
                    }

                    // Harness is first: full quick-settings mirror + mode grid.
                    // (Behavior was absorbed here so knobs aren't duplicated.)
                    if (s_aiSubSel == 0) {
                        settingsGlassSectionBegin("##ai_basic_models", "Models");
                        drawModelRolesSection(st, sc);
                        settingsGlassSectionEnd();
                        // Session knobs lead (most-used); the mode grid sits at the bottom
                        // of the page — it is a read-mostly orientation card.
                        settingsGlassSectionBegin("##ai_harness_session", "Session knobs");
                        dimText("Same knobs as Copilot -> Quick settings, plus modal-only "
                                "options such as role adoption.");
                        ImGui::Dummy(ImVec2(0, 4.0f * sc));
                        if (drawHarnessSessionKnobs(st, sc))
                            saveSettings(st.settings);
                        settingsGlassSectionEnd();

                        settingsGlassSectionBegin("##ai_memory", "Memory & Learning");
                        drawMemoryLearningSection(st, sc);
                        settingsGlassSectionEnd();

                        // Harness organization is a structural/diagnostic surface — the
                        // shipped arrangement is correct for normal use.
                        if (st.settings.showAdvanced) {
                            settingsGlassSectionBegin("##ai_harness_org",
                                                      "Harness organization");
                            drawHarnessOrganizationSection(st, sc);
                            settingsGlassSectionEnd();
                        }
                        // Basic mode hides the Tools page, whose only everyday control is
                        // runtime repair. Re-home it here so a broken runtime is always
                        // fixable; Advanced keeps it on Tools where it has always lived.
                        if (!st.settings.showAdvanced) {
                            ImGui::Dummy(ImVec2(0, 10.0f * sc));
                            drawRuntimeMaintenanceSection(st, sc);
                        }
                    }



                    if (s_aiSubSel == 1) {
                        // Model pickers first (compact table).
                        settingsGlassSectionBegin("##ai_model_roles", "Model roles");
                        drawModelRolesSection(st, sc);
                        settingsGlassSectionEnd();

                        // Vision and spatial routing are advanced overrides; the shared model
                        // roles above remain available in Basic.
                        if (st.settings.showAdvanced) {
                            settingsGlassSectionBegin("##ai_vision_routing",
                                                      "Vision & spatial routing");
                            if (drawVisionRoutingSection(st, sc)) saveSettings(st.settings);
                            settingsGlassSectionEnd();
                        }
                    }

                    if (s_aiSubSel == 2) {
                        // Full-height role prompt editor for power users / dev refinement.
                        const bool canFetch = st.workerStarted;
                        if (!s_promptsLoaded && canFetch) loadPrompts(st);

                        ImGui::AlignTextToFramePadding();
                        ImGui::TextUnformatted("Role prompts");
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip(
                                "Edit mission/YAML and native/core role prompts independently. "
                                "Overrides save to the user data prompts directory.");
                        ImGui::SameLine();
                        if (!canFetch || s_promptsLoading) ImGui::BeginDisabled();
                        if (ImGui::SmallButton("Refresh##prompts_refresh")) {
                            s_promptsLoaded = false;
                            loadPrompts(st);
                        }
                        if (!canFetch || s_promptsLoading) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                            ImGui::SetTooltip(!canFetch
                                ? "Start the worker to load role prompts"
                                : "Prompt catalog is loading");
                        if (!s_promptsError.empty()) {
                            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
                            ImGui::TextWrapped("%s", s_promptsError.c_str());
                            ImGui::PopStyleColor();
                        } else if (!s_promptsLoaded) {
                            ImGui::TextWrapped("%s", s_promptsLoading
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
                        const bool compactPrompts =
                            ImGui::GetContentRegionAvail().x < ui::metrics::px(450.0f);
                        if (compactPrompts) {
                            const char* preview = s_selectedRole < sysIdx && s_selectedRole >= 0
                                ? s_promptRecs[s_selectedRole].role.c_str() : "system (override)";
                            float widest = ImGui::CalcTextSize("system (override)").x;
                            for (const auto& pr : s_promptRecs)
                                widest = ImMax(widest, ImGui::CalcTextSize(pr.role.c_str()).x);
                            ImGui::SetNextItemWidth(-1);
                            if (settingsBeginPopupRowCombo("##prompt_role_pick", preview, widest)) {
                                for (int i = 0; i < sysIdx; ++i) {
                                    ImGui::PushID(i);
                                    if (settingsPopupRowSelectable(s_promptRecs[i].role.c_str(),
                                                                  s_selectedRole == i)) {
                                        s_selectedRole = i;
                                        s_promptSystemSelected = false;
                                        s_pendingPromptSelection.clear();
                                    }
                                    ImGui::PopID();
                                }
                                if (settingsPopupRowSelectable("system (override)",
                                                               s_selectedRole == sysIdx)) {
                                    s_selectedRole = sysIdx;
                                    s_promptSystemSelected = true;
                                    s_pendingPromptSelection.clear();
                                }
                                settingsEndPopupRowCombo();
                            }
                        }
                        // Fill remaining viewport — single scrollbar inside list/editor only.
                        const float promptsH = ImMax(220.0f * sc,
                            ImGui::GetContentRegionAvail().y - 4.0f * sc);
                        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(ui::metrics::px(10.0f), ui::metrics::px(8.0f)));
                        if (!compactPrompts) {
                        ImGui::PushStyleColor(ImGuiCol_ChildBg,
                            ImVec4(ui::palette.panelBg[0], ui::palette.panelBg[1],
                                   ui::palette.panelBg[2], 1));
                        ImGui::BeginChild("##rolelist", ImVec2(168.0f * sc, promptsH),
                                          ImGuiChildFlags_Borders);
                        if (s_promptRecs.empty() && s_promptsLoaded) {
                            dimText("No role prompts returned.");
                        }
                        for (int i = 0; i < (int)s_promptRecs.size(); ++i) {
                            bool sel = (i == s_selectedRole);
                            auto& pr = s_promptRecs[i];
                            std::string lbl = pr.role;
                            if (pr.overridden) lbl += " *";
                            if (settingsChildRowSelectable(lbl.c_str(), sel)) {
                                s_selectedRole = i;
                                s_promptSystemSelected = false;
                                s_pendingPromptSelection.clear();
                            }
                            if (sel) ImGui::SetItemDefaultFocus();
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("%s%s", pr.role.c_str(),
                                                  pr.overridden ? " (overridden)" : "");
                        }
                        ImGui::Separator();
                        {
                            std::string syslbl = "system (override)";
                            if (st.spBuf[0] != 0) syslbl += " *";
                            if (settingsChildRowSelectable(syslbl.c_str(), s_selectedRole >= sysIdx)) {
                                s_selectedRole = sysIdx;
                                s_promptSystemSelected = true;
                                s_pendingPromptSelection.clear();
                            }
                            if (s_selectedRole >= sysIdx) ImGui::SetItemDefaultFocus();
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Override the agent system prompt (advanced)");
                        }
                        ImGui::EndChild();
                        ImGui::PopStyleColor();  // ChildBg (list only — editor stays transparent)

                        ImGui::SameLine();
                        }

                        ImGui::BeginChild("##roleeditor", ImVec2(0, promptsH),
                                          ImGuiChildFlags_Borders);
                        if (s_selectedRole >= sysIdx) {
                            ImGui::PushFont(st.fonts.medium);
                            ImGui::TextUnformatted("System prompt override");
                            ImGui::PopFont();
                            dimText("Empty = built-in. Custom text fully replaces it \xe2\x80\x94 "
                                    "keep the result/parts contract. Autosaves as you type.");
                            ImGui::PushFont(st.fonts.mono);
                            float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                            if (edH < 100.0f * sc) edH = 100.0f * sc;
                            if (ImGui::InputTextMultiline("##sysprompt", st.spBuf.data(), st.spBuf.size(),
                                                      ImVec2(-1, edH))) {
                                s_spOverrideDirty = true;
                                s_spOverrideLastEdit = ImGui::GetTime();
                            }
                            ImGui::PopFont();
                            if (ui::kit::secondaryButton("Clear (use built-in)")) {
                                std::fill(st.spBuf.begin(), st.spBuf.end(), 0);
                                commitSystemPromptOverride(st);  // empty = built-in, persist now
                            }
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
                            dimText("Autosaves as you type.");
                            ImGui::PushFont(st.fonts.mono);
                            float edH = ImGui::GetContentRegionAvail().y - 38.0f * sc;
                            if (edH < 100.0f * sc) edH = 100.0f * sc;
                            if (ImGui::InputTextMultiline("##prompted", pr.buf.data(), pr.buf.size(),
                                                          ImVec2(-1, edH))) {
                                pr.dirty = true;
                                pr.lastEdit = ImGui::GetTime();
                            }
                            ImGui::PopFont();
                            const char* noFetch = "Prompts are unavailable until the engine is connected.";
                            // No Save button: the edit above autosaves (flushPromptEdits).
                            if (ui::kit::secondaryButton("Reset to built-in", canFetch, noFetch,
                                                         ImVec2(140.0f * sc, 28.0f * sc))) {
                                resetPromptOverride(pr, st);
                                st.addLog("[ok] prompt reset: " + pr.role);
                            }
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Delete override and restore shipped prompt");
                            if (!s_promptSaveError.empty()) {
                                ImGui::SameLine();
                                ImGui::TextColored(settingsErrInk(), "%s", s_promptSaveError.c_str());
                            }
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

                // The agent benchmark is the artifact_validity suite (benchmarks/
                // artifact_validity): a CLI runner that grades the saved deliverable file of
                // each trial. It has no in-app runner, so this card names the suite and hands
                // over the exact command instead of an empty table.
                settingsGlassSectionBegin("##bench_artifact", "Agent benchmark", 0.0f,
                                          /*first=*/true);
                static const char* kArtifactRunCmd =
                    "python -m benchmarks.artifact_validity.runner run --tag demo "
                    "--tasks F1,F2,F3,F4,C1,C2,C3,S1,S2,S3 --model <provider:model>";
                if (ui::kit::emptyState(
                        nullptr, "Artifact validity suite",
                        "Each trial gives the agent one headless turn in a fresh workspace, "
                        "then reopens the one file the task asks for and grades it. Tasks: "
                        "facilities F1-F4, core C1-C3, solids S1-S3. Run it from the "
                        "repository root with "
                        "python -m benchmarks.artifact_validity.runner run, then "
                        "report --tag <tag> for the scored table.",
                        "Copy run command"))
                    ImGui::SetClipboardText(kArtifactRunCmd);
                settingsGlassSectionEnd();

                // Legacy gym leaderboard, per-tool-family categories and quick runner: kept
                // reachable but folded away — the board is retired (often MOCK smoke data)
                // and must not read as the current score. A legacy run in flight forces it
                // open so its progress is never hidden.
                static bool s_showLegacyBench = false;
                ui::kit::checkbox("bench_show_legacy", "Show legacy results", &s_showLegacyBench,
                                  "The retired gym leaderboard, its tool-family categories and "
                                  "the legacy quick runner.");
                // Keep status polling alive while this tab is open.
                pollBenchStatus(st, s_bench.running, &s_lb);
                if (s_showLegacyBench || s_bench.running) {
                    pollBenchCategories(st, /*force=*/false);
                    if (!s_cats.cats.empty()) {
                        settingsGlassSectionBegin("##bench_categories", "Legacy categories");
                        drawBenchCategories(st, sc);
                        settingsGlassSectionEnd();
                    }

                    settingsGlassSectionBegin("##bench_leaderboard", "Legacy gym leaderboard");
                    dimText("Pass rates from the retired gym suite "
                            "(benchmarks/leaderboard/leaderboard.json). Superseded by the "
                            "artifact validity suite above.");
                    if (ui::kit::secondaryButton("Refresh##settings_lb")) {
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

                    }

                    if (s_aiSubSel == 6) {

                        settingsGlassSectionBegin("##ai_tools_global", "Global tool limits");
                        dimText("Defaults applied to every tool call. A per-tool Options override "
                                "(nonzero) always wins. Autosaved under config.json agent.*.");
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
                            // Never reload over unsaved edits (it clobbered them every 2 s).
                            if (!s_globalLoaded ||
                                (!s_globalDirty && (nowGl - s_globalLast) > 2.0)) {
                                s_globalLast = nowGl;
                                s_globalLoaded = true;
                                try {
                                    const nlohmann::json j = readConfigJson();
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
                        // 0 means "off / unlimited" for these limits: shown as an empty field
                        // with that word as the hint, never as a bare zero.
                        auto optionalLimit = [&](const char* id, const char* label, int* v,
                                                 const char* offHint, const char* unit,
                                                 const char* tip) {
                            bool on = *v > 0;
                            int shown = on ? *v : 1;
                            ui::kit::FieldOpts o;
                            o.withPresent(&on).withHint(offHint).withMin(1).withTip(tip);
                            if (unit) o.withUnit(unit);
                            if (ui::kit::fieldInt(id, label, &shown, o)) {
                                *v = on ? shown : 0;
                                s_globalDirty = true;
                            }
                        };
                        {
                            ui::kit::FieldGroup limits("global_limits", "Supervision interval");
                            optionalLimit("global_timeout_s", "Tool timeout", &s_globalTimeout,
                                          "No timeout", ui::kit::unitText(ui::kit::Unit::s),
                                          "Seconds before a tool call is abandoned. Empty = "
                                          "no timeout. A per-tool override wins.");
                            optionalLimit("global_max_calls", "Max calls per turn",
                                          &s_globalMaxCalls, "Unlimited", nullptr,
                                          "Calls of one tool allowed per turn. Empty = "
                                          "unlimited. A per-tool override wins.");
                            optionalLimit("global_supervision", "Supervision interval",
                                          &s_globalSupervision, "Never", nullptr,
                                          "Force an advisor verification pass every N tool "
                                          "calls. Empty = never.");
                            if (ui::kit::fieldCheckbox(
                                    "loop_guard_enabled", "Loop guard", &s_loopGuardEnabled,
                                    ui::kit::FieldOpts().withTip(
                                        "Refuse a tool call that repeats the exact same tool + "
                                        "arguments too many times in a row instead of "
                                        "dispatching it again (a real stuck loop, not just "
                                        "\"used this tool a lot\").")))
                                s_globalDirty = true;
                            if (s_loopGuardEnabled &&
                                ui::kit::fieldInt("loop_guard_threshold", "Repeats before refusal",
                                                  &s_loopGuardThreshold,
                                                  ui::kit::FieldOpts().withMin(1).withTip(
                                                      "Consecutive identical calls before "
                                                      "refusal (default 3).")))
                                s_globalDirty = true;
                        }
                        if (s_globalDirty) {
                            // Autosave: commit the edit on the frame it happened (the writer
                            // coalesces keystrokes). A rejected patch stays dirty and retries.
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
                            } else {
                                s_globalErr = "Failed to save global tool limits.";
                            }
                        }
                        if (!s_globalErr.empty())
                            ImGui::TextColored(settingsErrInk(), "%s", s_globalErr.c_str());
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
                        static bool s_liveCatLoading = false;
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
                        // (retry every 15 s until it succeeds). The generated catalog remains
                        // usable while the queued request waits behind an active agent run.
                        if (st.workerStarted && !s_liveCatLoading &&
                            (!s_liveCatTried ||
                             (!s_liveCatOk && ImGui::GetTime() - s_liveCatAt > 15.0))) {
                            s_liveCatTried = true;
                            s_liveCatLoading = true;
                            s_liveCatAt = ImGui::GetTime();
                            st.rpc.call("ai_tool_catalog", nlohmann::json::object(),
                                        [](const core::RpcResult& r) {
                                s_liveCatLoading = false;
                                if (!r.ok || !r.result.value("ok", false)) {
                                    s_liveCatAt = ImGui::GetTime();
                                    return;
                                }
                                s_liveFamTools.clear();
                                s_liveDescs.clear();
                                s_liveGuidance = nlohmann::json::object();
                                for (const auto& row :
                                     r.result.value("tools", nlohmann::json::array())) {
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
                            }, 20000);
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
                                const nlohmann::json j = readConfigJson();
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
                                       "surface (unioned onto the role card allowlist).");

                            std::set<std::string>& assigned = s_roleTools[rk];
                            if (ImGui::BeginTable("##role_fam_assign", 3,
                                                  ImGuiTableFlags_SizingStretchSame |
                                                  ImGuiTableFlags_NoPadOuterX)) {
                                for (int fi = 0; fi < kToolFamilyCount; ++fi) {
                                    const char* fam = kToolFamilies[fi];
                                    ImGui::TableNextColumn();
                                    bool on = assigned.count(fam) > 0;
                                    ImGui::PushID(fam);
                                    if (ui::kit::checkbox("fam", fam, &on)) {
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

                        settingsSectionHeader("Agent service built-ins");
                        {
                            static std::vector<ui::OmpToolRow> tools;
                            static std::string forSession;
                            static bool loaded = false, inFlight = false, failed = false;
                            static double lastPoll = -1e9;
                            const std::string conv = st.activeConvId();
                            const auto* link = ui::ompSessionLink(conv);
                            const bool live = link && link->runtimeLive;
                            if (forSession != conv) {
                                forSession = conv;
                                loaded = false;
                                failed = false;
                                lastPoll = -1e9;
                                tools.clear();
                            }
                            const double now = ImGui::GetTime();
                            if (live && !inFlight && now - lastPoll > (loaded ? 60.0 : 15.0)) {
                                lastPoll = now;
                                inFlight = true;
#ifdef __EMSCRIPTEN__
                                const std::string session = cellAgentSessionHandle(conv);
#else
                                const std::string session = conv;
#endif
                                st.rpc.call("omp_tool_state", nlohmann::json{{"session_id", session}},
                                    [conv](const core::RpcResult& r) {
                                        inFlight = false;
                                        if (forSession != conv) return;
                                        std::vector<ui::OmpToolRow> rows;
                                        failed = !r.ok || !ui::parseOmpToolState(r.result, rows);
                                        if (!failed) {
                                            tools = std::move(rows);
                                            loaded = true;
                                        }
                                    }, 15000);
                            }
                            if (!live) dimText("No live agent session.");
                            else if (failed) dimText("Built-in tool state could not be refreshed.");
                            else if (!loaded) dimText("Reading built-in tool state...");
                            else if (tools.empty()) dimText("The runtime reported no tools.");
                            else for (const auto& tool : tools) {
                                ImGui::PushID(tool.name.c_str());
                                ompStatusDotText(tool.name.c_str(),
                                    tool.availabilityKnown && tool.available ? ui::palette.statusOk
                                                                            : ui::palette.textDim,
                                    tool.availabilityKnown && tool.available);
                                if (!tool.availabilityKnown) {
                                    ImGui::SameLine();
                                    dimText("availability unknown");
                                } else if (!tool.available) {
                                    ImGui::SameLine();
                                    dimText("unavailable");
                                }
                                if (!tool.description.empty() && ImGui::IsItemHovered())
                                    ImGui::SetTooltip("%s", tool.description.c_str());
                                ImGui::PopID();
                            }
                        }

                        ImGui::Dummy(ImVec2(0, 8.0f * sc));
                        dimText("Per-tool rows: checkbox enables a tool globally (dimmed = off); "
                                "Options overrides limits, included tool context, and AI usage "
                                "guidance. Hover a name for what it does.");
                        if (s_liveCatLoading)
                            dimText("Loading live tool catalog; showing built-in catalog meanwhile.");
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
                                if (ui::kit::checkbox("en", "", &en)) {
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
                                    ImGui::TextDisabled("Empty = use the built-in or global "
                                                        "default shown in the field.");
                                    const float boxW = 360.0f * sc;
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
                                    // An unset override (0) is an empty field whose hint names
                                    // the value it inherits, never a bare zero.
                                    const int curT = curated.value("timeout_s", 0);
                                    const int curM = curated.value("max_calls_per_turn", 0);
                                    const std::string toHint =
                                        curT > 0 ? "Default " + std::to_string(curT) + " s"
                                        : s_globalTimeout > 0
                                            ? "Global " + std::to_string(s_globalTimeout) + " s"
                                            : std::string("No timeout");
                                    const std::string mcHint =
                                        curM > 0 ? "Default " + std::to_string(curM)
                                        : s_globalMaxCalls > 0
                                            ? "Global " + std::to_string(s_globalMaxCalls)
                                            : std::string("Unlimited");
                                    {
                                        ui::kit::FieldGroup limits("tool_limits",
                                                                   "Max calls per turn");
                                        bool toSet = to > 0;
                                        int toShown = toSet ? to : 1;
                                        if (ui::kit::fieldInt(
                                                "timeout_s", "Timeout", &toShown,
                                                ui::kit::FieldOpts()
                                                    .withPresent(&toSet)
                                                    .withHint(toHint.c_str())
                                                    .withMin(1)
                                                    .withUnit(ui::kit::Unit::s))) {
                                            s_toolTimeout[tn] = toSet ? toShown : 0;
                                            s_toolOptDirty = true;
                                        }
                                        bool mcSet = mc > 0;
                                        int mcShown = mcSet ? mc : 1;
                                        if (ui::kit::fieldInt(
                                                "max_calls_per_turn", "Max calls per turn",
                                                &mcShown,
                                                ui::kit::FieldOpts()
                                                    .withPresent(&mcSet)
                                                    .withHint(mcHint.c_str())
                                                    .withMin(1))) {
                                            s_toolMaxCalls[tn] = mcSet ? mcShown : 0;
                                            s_toolOptDirty = true;
                                        }
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
                                    const char* trimLabels[] = {
                                        "Inherit tier", "Full paragraph", "First sentence"
                                    };
                                    // Map stored -1/0/1 -> items 0/1/2
                                    int trimSel = (trimMode < 0) ? 0 : (trimMode == 0 ? 1 : 2);
                                    if (ui::kit::fieldEnum(
                                            "schema_trim", "Description length", &trimSel,
                                            trimLabels, 3,
                                            ui::kit::FieldOpts()
                                                .withEnumStyle(ui::kit::EnumStyle::Combo)
                                                .withTip("How much of the tool description is "
                                                         "included in the AI tools prompt. "
                                                         "Inherit = tier default."))) {
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
                                    static bool s_previewFetching = false;
                                    const bool canPrev = st.workerStarted;
                                    auto makeLocalPreview = [&]() {
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
                                        return "[TOOLS] Call tools with EXACTLY these kwargs:\n" +
                                               entry;
                                    };
                                    auto buildLocalPreview = [&]() {
                                        s_previewText = makeLocalPreview();
                                        s_previewErr.clear();
                                    };
                                    auto fetchPreview = [&](bool force) {
                                        const double now = ImGui::GetTime();
                                        if (s_previewFetching ||
                                            (!force && s_previewTool == tn &&
                                             (now - s_previewAt) < 1.5))
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
                                        const std::string localPreview = makeLocalPreview();
                                        s_previewFetching = true;
                                        flushConfigWrites();
                                        st.rpc.call("ai_tool_preview", std::move(p),
                                                    [localPreview](const core::RpcResult& r) {
                                            s_previewFetching = false;
                                            if (!r.ok || !r.result.value("ok", false)) {
                                                s_previewText = localPreview;
                                                s_previewErr = r.err.empty()
                                                    ? r.result.value(
                                                        "error", std::string("preview failed"))
                                                    : r.err;
                                                return;
                                            }
                                            s_previewText = r.result.value("text", "");
                                            if (s_previewText.empty())
                                                s_previewText = r.result.value("entry", "");
                                            s_previewErr.clear();
                                        }, 8000);
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
                                    if (!canPrev || s_previewFetching) ImGui::BeginDisabled();
                                    if (ImGui::SmallButton("Refresh##tool_preview"))
                                        fetchPreview(/*force=*/true);
                                    if (!canPrev || s_previewFetching) ImGui::EndDisabled();
                                    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                                        ImGui::SetTooltip(s_previewFetching
                                            ? "Loading tool preview"
                                            : "Start the worker to refresh the preview");
                                    // Always refresh local approx when worker unavailable.
                                    if (!canPrev && (s_previewTool != tn || s_toolOptDirty))
                                        buildLocalPreview();
                                    if (s_previewFetching) {
                                        ImGui::SameLine();
                                        dimText("Loading\xe2\x80\xa6");
                                    }
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

                        // Autosave. Every edit above flags its half (role families / per-tool
                        // options) dirty; the commit happens on that same frame so a sub-tab
                        // switch or close cannot strand it. Each half writes only its own keys:
                        // a tool-option edit must not also materialise role_tools /
                        // tool_families from the in-memory defaults.
                        if (s_roleToolsDirty || s_toolOptDirty) {
                            bool saveRoles = s_roleToolsDirty;
                            bool saveOpts = s_toolOptDirty;
                            nlohmann::json rt = nlohmann::json::object();
                            nlohmann::json tf = nlohmann::json::object();
                            nlohmann::json to = nlohmann::json::object();
                            if (saveRoles) {
                                for (const auto& kv : s_roleTools) {
                                    nlohmann::json arr = nlohmann::json::array();
                                    for (const auto& fam : kv.second) arr.push_back(fam);
                                    rt[kv.first] = std::move(arr);
                                }

                                // Derive tool_families as the union of all role assignments
                                // so legacy kill-switches / MCP external clients stay coherent.
                                std::set<std::string> unionFams;
                                for (const auto& kv : s_roleTools)
                                    for (const auto& fam : kv.second) unionFams.insert(fam);
                                for (int i = 0; i < kToolFamilyCount; ++i)
                                    tf[kToolFamilies[i]] = unionFams.count(kToolFamilies[i]) > 0;
                            }
                            s_toolOptErr.clear();
                            if (saveOpts) {
                                std::set<std::string> names;
                                for (const auto& kv : s_toolEnabled) names.insert(kv.first);
                                for (const auto& kv : s_toolTimeout) names.insert(kv.first);
                                for (const auto& kv : s_toolMaxCalls) names.insert(kv.first);
                                for (const auto& kv : s_toolDefaults) names.insert(kv.first);
                                for (const auto& kv : s_toolNote) names.insert(kv.first);
                                for (const auto& kv : s_toolDescOverride) names.insert(kv.first);
                                for (const auto& kv : s_toolSchemaTrim) names.insert(kv.first);
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
                                            s_toolOptErr = "Invalid JSON in param_defaults for " + tn +
                                                           " \xe2\x80\x94 tool options are not saved until it is fixed.";
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
                                // Half-typed JSON must not wipe the stored options: they wait
                                // (still dirty) until it parses; the role half saves regardless.
                                if (!s_toolOptErr.empty()) saveOpts = false;
                            }
                            if (saveRoles || saveOpts) {
                                const bool ok = updateConfigJson([&](nlohmann::json& j) {
                                    if (!j.contains("agent") || !j["agent"].is_object())
                                        j["agent"] = nlohmann::json::object();
                                    if (saveRoles) {
                                        j["agent"]["role_tools"] = rt;
                                        // Merge, never replace: tool_families also carries keys
                                        // this page has no control for (e.g. manufacture, org).
                                        auto& fams = j["agent"]["tool_families"];
                                        if (!fams.is_object()) fams = nlohmann::json::object();
                                        for (auto it = tf.begin(); it != tf.end(); ++it)
                                            fams[it.key()] = it.value();
                                    }
                                    if (saveOpts) j["agent"]["tool_options"] = to;
                                });
                                if (ok) {
                                    if (saveRoles) s_roleToolsDirty = false;
                                    if (saveOpts) s_toolOptDirty = false;
                                } else {
                                    s_toolOptErr = "Failed to save tool settings; retrying.";
                                }
                            }
                        }
                        ImGui::Dummy(ImVec2(0, 6.0f * sc));
                        if (!s_toolOptErr.empty())
                            ImGui::TextColored(settingsErrInk(), "%s", s_toolOptErr.c_str());
                        if (ui::kit::secondaryButton("Reset role defaults", true, nullptr, ImVec2(160.0f * sc, 0))) {
                            seedDefaultRoleTools();
                            s_roleToolsDirty = true;  // autosaved next frame
                        }
                        ImGui::SameLine();
                        if (ui::kit::secondaryButton("Reset tool options", true, nullptr, ImVec2(150.0f * sc, 0))) {
                            s_toolEnabled.clear();
                            s_toolTimeout.clear();
                            s_toolMaxCalls.clear();
                            s_toolDefaults.clear();
                            s_toolNote.clear();
                            s_toolDescOverride.clear();
                            s_toolSchemaTrim.clear();
                            s_toolOptDirty = true;  // autosaved next frame
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
                            if (ui::kit::checkbox("bug_reports", "Send bug reports", &s_bugReports))
                                saveAgentConfigValue("bug_reports", s_bugReports);
                            if (ImGui::IsItemHovered())
                                ImGui::SetTooltip("Saved immediately to config.json "
                                                  "agent.bug_reports.");
                        }
                        settingsGlassSectionEnd();

                        ImGui::Dummy(ImVec2(0, 10.0f * sc));
                        drawRuntimeMaintenanceSection(st, sc);
                    }

                    // ---- Dev Preview: assembled general_agent system prompt ----
                    if (s_aiSubSel == 7) {
                        if (aiSubTabJustEntered(7)) ImGui::SetScrollY(0.0f);
                        settingsGlassSectionBegin("##ai_dev_preview", "Prompt assembly preview");
                        dimText("Shows the system prompt a typical main-chat / general_agent "
                                "turn would send (effective role/global override + capability "
                                "SOFT block + harness base + catalogs + tools slot + Auto-Learn "
                                "guidance when enabled), followed by an example user message "
                                "that marks where the request goes. Nothing here is sent to a "
                                "model.");

                        static std::string s_devSys;
                        static std::string s_devUser;
                        static std::string s_devErr;
                        static std::string s_devMeta;
                        static bool s_devLoaded = false;
                        static bool s_devFetching = false;
                        static bool s_includeTools = true;

                        const bool canDev = st.workerStarted;
                        auto fetchDevPreview = [&]() {
                            if (s_devFetching) return;
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
                            const bool includeTools = s_includeTools;
                            flushConfigWrites();
                            st.rpc.call("ai_prompt_preview",
                                        nlohmann::json{{"include_tools", includeTools}},
                                        [includeTools](const core::RpcResult& r) {
                                s_devFetching = false;
                                if (s_includeTools != includeTools) {
                                    s_devLoaded = false;
                                    return;
                                }
                                if (!r.ok || !r.result.value("ok", false)) {
                                    s_devSys.clear();
                                    s_devUser.clear();
                                    s_devMeta.clear();
                                    s_devErr = r.err.empty()
                                        ? r.result.value(
                                            "error", std::string("ai_prompt_preview failed"))
                                        : r.err;
                                    s_devLoaded = true;
                                    return;
                                }
                                // Fold once here, not per frame: the prompt is long and may
                                // carry glyphs the UI font lacks.
                                s_devSys = ui::foldToAtlasGlyphs(r.result.value("system", ""));
                                // The worker brackets its sample request with a color-span
                                // marker ("[[USER REQUEST - sample]] ", math brackets); the
                                // preview shows the example request itself under its own
                                // caption instead. Marker bytes are matched, never rendered.
                                std::string user = r.result.value("user_placeholder", "");
                                static const char* kMarkClose = "\xe2\x9f\xa7";  // gui-lint: allow
                                const size_t close = user.find(kMarkClose);
                                if (user.rfind("\xe2\x9f\xa6", 0) == 0 &&  // gui-lint: allow
                                    close != std::string::npos) {
                                    user.erase(0, close + std::strlen(kMarkClose));
                                    user.erase(0, user.find_first_not_of(' '));
                                }
                                s_devUser = ui::foldToAtlasGlyphs(user);
                                auto meta = r.result.value("meta", nlohmann::json::object());
                                char buf[256];
                                std::snprintf(buf, sizeof(buf),
                                    "%s  \xc2\xb7  %d tools  \xc2\xb7  %d chars  \xc2\xb7  "
                                    "~%d tokens  \xc2\xb7  Auto-Learn %s",
                                    meta.value("role", "general_agent").c_str(),
                                    meta.value("allowed_tool_count", 0),
                                    meta.value("system_chars", 0),
                                    meta.value("system_tokens_est", 0),
                                    meta.value("autolearn", false) ? "on" : "off");
                                s_devMeta = buf;
                                s_devErr.clear();
                                s_devLoaded = true;
                            }, 30000);
                        };

                        if (!s_devLoaded && canDev && !s_devFetching)
                            fetchDevPreview();

                        if (s_devFetching) ImGui::BeginDisabled();
                        if (ui::kit::checkbox("dev_tools_slot", "Include tools slot", &s_includeTools)) {
                            s_devLoaded = false;
                        }
                        if (s_devFetching) ImGui::EndDisabled();
                        ImGui::SameLine();
                        if (!canDev || s_devFetching) ImGui::BeginDisabled();
                        if (ui::kit::secondaryButton("Refresh##dev_preview", true, nullptr, ImVec2(100.0f * sc, 0)))
                            fetchDevPreview();
                        if (!canDev || s_devFetching) ImGui::EndDisabled();
                        if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                            ImGui::SetTooltip(s_devFetching
                                ? "Assembling prompt preview"
                                : "Start the worker to load the prompt preview");
                        if (s_devFetching) {
                            ImGui::SameLine();
                            dimText("Loading\xe2\x80\xa6");
                        }
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

                        // System prompt (normal ink) then the example user message (accent).
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
                        // Section captions in the UI font, not mono rule lines.
                        auto caption = [&](const char* text) {
                            ImGui::PopFont();
                            dimText(text);
                            ImGui::PushFont(st.fonts.mono);
                        };
                        if (!s_devSys.empty()) {
                            caption("System prompt");
                            ImGui::TextUnformatted(s_devSys.c_str());
                            if (!s_devUser.empty()) {
                                ImGui::Dummy(ImVec2(0, 10.0f * sc));
                                caption("Example user message (not sent)");
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ui::Palette::toU32(ui::palette.accent));
                                ImGui::TextWrapped("%s", s_devUser.c_str());
                                ImGui::PopStyleColor();
                            }
                        } else if (s_devFetching || (!s_devLoaded && canDev)) {
                            caption("Assembling the prompt\xe2\x80\xa6");
                        } else {
                            ImGui::PopFont();
                            ui::kit::emptyState(
                                nullptr, "No preview yet",
                                canDev ? "Refresh to assemble the prompt a main-chat turn "
                                         "would send."
                                       : "Start the engine to assemble the prompt a main-chat "
                                         "turn would send.");
                            ImGui::PushFont(st.fonts.mono);
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
            }

            // ---- Providers page: two-pane provider manager ----
            if (s_settingsPage == 3) {
                providerTabDrawn = true;
                settingsTabBodyBegin("##tab_providers");
                dimText("Provider sign-in and model availability. New credentials are stored "
                        "by the runtime; availability does not verify provider access.");
                // Online model limits and thinking are derived from OMP capabilities.
                drawOmpProviderManager(st);

                settingsTabBodyEnd();
            }
        }
        if (!providerTabDrawn) leaveOmpProviderView();
        ImGui::EndChild();
        // Nested modal: must be submitted every frame inside the Settings window scope or
        // ImGui's modal stack makes it non-interactive. It clears the flag itself on close.
        ui::drawRulesAuthoringPopup(st, &s_rulesAuthorOpen);
        }
        ImGui::End();
        ImGui::PopStyleVar();  // WindowPadding
    }

    // Modal closed (X) with the system-prompt debounce still pending: force-commit it.
    if (!s_settingsOpen) {
        if (s_settingsEnabledKeyboardNav) {
            ImGui::GetIO().ConfigFlags &= ~ImGuiConfigFlags_NavEnableKeyboard;
            s_settingsEnabledKeyboardNav = false;
        }
        flushPromptEditors(st, /*force=*/true);
        flushSkillRuleEdits(st, /*force=*/true);
        if (s_wsDirty) commitWorkspaceDir();
        if (s_boxDirty) commitBoxSettings(/*refreshStatus=*/false);
        if (s_omp.modalOpen) closeOmpProviderModal(st);
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
        // Product version + short sha: the first thing a support request needs, and
        // the only place a GUI-only user can read it. Same string `chudware --version`
        // prints (api/chudware_version.h, generated at configure time).
        ImGui::Text("Version %s (%s)", CHUDWARE_VERSION, CHUDWARE_GIT_SHA);
#if CHUDWARE_BUILD_DIRTY
        dimText("built from a modified working tree");
#endif
#ifndef __EMSCRIPTEN__
        {
            const auto update = chudware::update::globalUpdater().snapshot();
            if (update.state == chudware::update::State::Available ||
                update.state == chudware::update::State::ManualDownload ||
                update.state == chudware::update::State::Staged) {
                const std::string updateLine =
                    update.state == chudware::update::State::Staged
                        ? "Update " + update.version + " is verified; restart to install"
                        : update.message;
                ImGui::TextWrapped("%s", updateLine.c_str());
                if (!update.downloadUrl.empty() &&
                    ImGui::SmallButton("Open update download"))
                    ui::openExternalHttpUrl(update.downloadUrl);
            } else if (update.state == chudware::update::State::Checking) {
                dimText("Checking for updates\xe2\x80\xa6");
            } else if (update.state == chudware::update::State::Current) {
                dimText("Chudware is up to date");
            } else if (update.state == chudware::update::State::Error) {
                dimText(update.message.c_str());
            }
            // Manual re-check. The worker polls on a long interval, so this is the
            // only way to pick up a release published since launch without a
            // restart. Idle means no worker is running (headless capture, or the
            // updater was never configured), so there is nothing to wake.
            if (update.state != chudware::update::State::Idle) {
                const bool inFlight = update.state == chudware::update::State::Checking ||
                                      update.state == chudware::update::State::Downloading;
                ImGui::BeginDisabled(inFlight);
                if (ImGui::SmallButton("Check now##desktop_update_check"))
                    chudware::update::globalUpdater().requestCheckNow();
                ImGui::EndDisabled();
            }
        }
#endif
        ImGui::Dummy(ImVec2(0, 4 * sc));
        ImGui::TextUnformatted("build123d + OpenCASCADE kernel");
        ImGui::TextUnformatted("GLFW / Dear ImGui / OpenGL");
        ImGui::Dummy(ImVec2(0, 8 * sc));
        if (primaryButton("Close", ImVec2(110.0f * sc, 32.0f * sc))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }

    // Submitted last so the first-run flow stays topmost over Settings/About.
    ui::drawOnboarding(st, busy);
}

}  // namespace ui
