// dim_review.cpp — RE-22: dimension-confirm review UI + deviation view.
//
// ─── Architecture ────────────────────────────────────────────────────────────
// DimReviewState  — per-row FSM (idle → editing → pending → accepted/rejected)
//                   stored in the module-local singleton DRS().
// DeviationState  — async-loaded deviation result from get_reveng_deviation.
//
// All RPC calls are async (st.rpc.call(..., callback)) — the callback fires on
// the GL thread via the worker drain loop. No sync calls in draw paths.
// ─────────────────────────────────────────────────────────────────────────────
#include "dim_review.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "reveng_internal.h"  // S() → RevEngState, SessionSummary

namespace ui {
namespace reveng {

// ─── Row FSM ─────────────────────────────────────────────────────────────────

enum class RowState { Idle, Editing, Pending, Accepted, Rejected };

struct DimRow {
    std::string key;           // e.g. "output_shaft.dia"
    double      value_mm  = 0.0;
    std::string source;        // "photo_fiducial" | "datasheet" | "fitted" | "guessed"
    std::string tier;          // "measured" | "estimated" | "guessed"
    bool        required  = false;

    RowState    rowState  = RowState::Idle;
    double      editValue = 0.0;   // value in the edit box
    char        editBuf[32] = {};  // ImGui input buffer

    void beginEdit() {
        rowState  = RowState::Editing;
        editValue = value_mm;
        std::snprintf(editBuf, sizeof(editBuf), "%.4g", value_mm);
    }
    void cancelEdit() {
        rowState = RowState::Idle;
    }
    void commitEdit() {
        // Parse editBuf → editValue; tolerate garbage (keep old).
        char* end = nullptr;
        double v = std::strtod(editBuf, &end);
        if (end && end != editBuf && v > 0.0) editValue = v;
        rowState = RowState::Pending;
    }
};

// ─── Deviation result ────────────────────────────────────────────────────────

struct DeviationBucket {
    std::string label;
    double      fraction = 0.0;  // 0-1
    double      max_mm   = 0.0;
};

struct DeviationResult {
    bool   ok       = false;
    double max_mm   = 0.0;
    double rms_mm   = 0.0;
    std::string reason;          // non-empty when ok==false
    std::vector<DeviationBucket> buckets;
};

// ─── Module state singleton ──────────────────────────────────────────────────

struct DimReviewState {
    // Rows loaded from confirm_dimensions payload or canned seed.
    std::vector<DimRow> rows;
    std::string         sessionId;    // session the rows belong to
    bool                rowsLoaded   = false;

    // Submit bookkeeping.
    bool submitPending = false;
    std::string submitErr;
    bool submitOk      = false;

    // Async load of confirm_dimensions demands (list_reveng_dims, or canned).
    bool loadPending   = false;
    bool loadedOnce    = false;
    double lastLoadT   = -100.0;
    std::string loadErr;

    // Deviation panel state.
    DeviationResult deviation;
    bool devPending    = false;
    bool devLoaded     = false;
    double lastDevT    = -100.0;
    std::string devErr;
};

static DimReviewState& DRS() {
    static DimReviewState s;
    return s;
}

// ─── Canned payload (headless shot / demo) ───────────────────────────────────

void seedCannedDimPayload() {
    DimReviewState& d = DRS();
    d.rows.clear();

    auto addRow = [&](const char* key, double val, const char* src, const char* tier, bool req) {
        DimRow r;
        r.key      = key;
        r.value_mm = val;
        r.source   = src;
        r.tier     = tier;
        r.required = req;
        d.rows.push_back(r);
    };
    addRow("output_shaft.dia",     8.0,  "photo_fiducial", "measured",  true);
    addRow("mount_holes.spacing",  18.0, "datasheet",      "measured",  true);
    addRow("body.length",          52.3, "fitted",         "estimated", false);
    addRow("body.width",           24.1, "fitted",         "estimated", false);
    addRow("wall.thickness",        2.0, "guessed",        "guessed",   false);

    d.sessionId  = "re_canned00";
    d.rowsLoaded = true;
    d.loadedOnce = true;
    d.lastLoadT  = 1e9;   // block live refetch for the whole canned session (shot/demo)
    d.loadErr.clear();

    // Seed a canned deviation result too.
    DeviationResult& dev = d.deviation;
    dev.ok     = true;
    dev.max_mm = 0.42;
    dev.rms_mm = 0.11;
    dev.reason.clear();
    dev.buckets = {
        {"< 0.1 mm",  0.61, 0.10},
        {"0.1–0.2 mm",0.22, 0.20},
        {"0.2–0.5 mm",0.14, 0.50},
        {"> 0.5 mm",  0.03, 0.42},
    };
    d.devLoaded = true;
    d.lastDevT  = 1e9;    // block live refetch — canned data must survive the shot
}

// ─── Async loaders ───────────────────────────────────────────────────────────

// Parse rows from the confirm_dimensions demands JSON returned by list_reveng_dims
// (or synthesised from the session's pending_confirmations field).
static void parseRowsFromJson(const nlohmann::json& j, DimReviewState& d) {
    d.rows.clear();
    // Expected shape: { ok: true, dims: [{key,value_mm,source,tier,required}, ...] }
    if (!j.contains("dims") || !j["dims"].is_array()) return;
    for (const auto& item : j["dims"]) {
        if (!item.is_object()) continue;
        DimRow r;
        r.key      = item.value("key", std::string());
        r.value_mm = item.value("value_mm", 0.0);
        r.source   = item.value("source", std::string("guessed"));
        r.tier     = item.value("tier",   std::string("guessed"));
        r.required = item.value("required", false);
        if (!r.key.empty()) d.rows.push_back(r);
    }
}

// Request the pending confirm_dimensions demands for the selected session.
// Falls back to canned payload when the tool is not registered or call fails.
static void loadDims(AppState& st) {
    if (!st.workerStarted) return;
    DimReviewState& d = DRS();
    if (d.loadPending) return;
    const RevEngState& rs = S();
    if (rs.selected < 0 || rs.selected >= (int)rs.sessions.size()) return;
    const std::string sid = rs.sessions[rs.selected].session_id;
    if (sid == d.sessionId && d.loadedOnce) return;  // already up-to-date

    const double now = ImGui::GetTime();
    if (d.loadedOnce && now - d.lastLoadT < 5.0) return;

    d.loadPending = true;
    d.lastLoadT   = now;
    d.sessionId   = sid;

    nlohmann::json args;
    args["session_id"] = sid;
    st.rpc.call("list_reveng_dims", args,
                [](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        d2.loadPending = false;
        d2.loadedOnce  = true;
        if (!r.ok) {
            // Tool not registered yet — seed canned demo payload so the UI is useful.
            seedCannedDimPayload();
            d2.loadErr = r.err.empty() ? "list_reveng_dims unavailable (demo mode)"
                                       : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            seedCannedDimPayload();
            d2.loadErr = r.result.is_object()
                ? r.result.value("reason", std::string("list_reveng_dims failed (demo mode)"))
                : std::string("list_reveng_dims failed (demo mode)");
            return;
        }
        d2.loadErr.clear();
        parseRowsFromJson(r.result, d2);
        d2.rowsLoaded = true;
    });
}

// Request deviation for the selected session (throttled, non-overlapping).
static void loadDeviation(AppState& st) {
    if (!st.workerStarted) return;
    DimReviewState& d = DRS();
    if (d.devPending) return;
    const RevEngState& rs = S();
    if (rs.selected < 0 || rs.selected >= (int)rs.sessions.size()) return;

    const double now = ImGui::GetTime();
    if (d.devLoaded && now - d.lastDevT < 10.0) return;

    const std::string sid = rs.sessions[rs.selected].session_id;
    // Skip if session changed; deviation for old session is stale.
    if (d.devLoaded && sid != d.sessionId) d.devLoaded = false;

    d.devPending = true;
    d.lastDevT   = now;

    nlohmann::json args;
    args["session_id"] = sid;
    // ref_path omitted — the tool handles the "no ref_path" error gracefully and
    // the UI shows it in devErr. The user can supply a ref mesh via the properties pane.
    st.rpc.call("get_reveng_deviation", args,
                [](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        d2.devPending = false;
        d2.devLoaded  = true;
        d2.lastDevT   = ImGui::GetTime();

        if (!r.ok) {
            d2.deviation.ok = false;
            d2.deviation.reason = r.err.empty() ? "worker unreachable" : r.err;
            d2.devErr = d2.deviation.reason;
            return;
        }
        if (!r.result.is_object()) {
            d2.deviation.ok = false;
            d2.deviation.reason = "malformed response";
            d2.devErr = d2.deviation.reason;
            return;
        }
        d2.devErr.clear();
        DeviationResult& dev = d2.deviation;
        dev.ok     = r.result.value("ok", false);
        dev.reason = r.result.value("reason", std::string());
        dev.max_mm = r.result.value("max_mm", 0.0);
        dev.rms_mm = r.result.value("rms_mm", 0.0);
        dev.buckets.clear();
        if (r.result.contains("buckets") && r.result["buckets"].is_array()) {
            for (const auto& b : r.result["buckets"]) {
                if (!b.is_object()) continue;
                DeviationBucket bk;
                bk.label    = b.value("label",    std::string());
                bk.fraction = b.value("fraction", 0.0);
                bk.max_mm   = b.value("max_mm",   0.0);
                dev.buckets.push_back(bk);
            }
        }
    });
}

// ─── Submit accepted rows ─────────────────────────────────────────────────────

static void submitAccepted(AppState& st) {
    DimReviewState& d = DRS();
    if (d.submitPending) return;

    nlohmann::json confirmations = nlohmann::json::array();
    for (auto& row : d.rows) {
        if (row.rowState == RowState::Pending || row.rowState == RowState::Accepted) {
            nlohmann::json c;
            c["key"]      = row.key;
            c["value_mm"] = (row.rowState == RowState::Pending) ? row.editValue : row.value_mm;
            confirmations.push_back(c);
            row.rowState = RowState::Accepted;
        }
    }
    if (confirmations.empty()) return;

    nlohmann::json args;
    args["confirmations"] = confirmations;
    d.submitPending = true;
    d.submitErr.clear();

    st.rpc.call("confirm_dimensions", args,
                [](const core::RpcResult& r) {
        DimReviewState& d2 = DRS();
        d2.submitPending = false;
        if (!r.ok) {
            d2.submitErr = r.err.empty() ? "confirm_dimensions call failed" : r.err;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            d2.submitErr = r.result.is_object()
                ? r.result.value("error", r.result.value("reason",
                    std::string("confirm_dimensions returned ok:false")))
                : std::string("confirm_dimensions returned ok:false");
            return;
        }
        d2.submitOk  = true;
        d2.submitErr.clear();
    });
}

// ─── Source tier badge colour ─────────────────────────────────────────────────

static ImVec4 tierColour(const std::string& tier) {
    if (tier == "measured")  return ImVec4(0.25f, 0.75f, 0.35f, 1.0f);  // green
    if (tier == "estimated") return ImVec4(0.95f, 0.65f, 0.10f, 1.0f);  // amber
    return ImVec4(0.80f, 0.30f, 0.25f, 1.0f);                           // red (guessed)
}

// ─── drawDimReviewPanel ───────────────────────────────────────────────────────

void drawDimReviewPanel(AppState& st) {
    const float sc = ui::uiScale;

    // Kick async loads.
    loadDims(st);

    DimReviewState& d = DRS();

    // Header.
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.accent[0], palette.accent[1],
                                                palette.accent[2], 1.0f));
    ImGui::TextUnformatted("Dimension Review");
    ImGui::PopStyleColor();
    if (!d.loadErr.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("  (%s)", d.loadErr.c_str());
    }
    ImGui::Separator();

    if (d.loadPending && !d.loadedOnce) {
        ImGui::TextDisabled("Loading dimensions…");
        return;
    }
    if (d.rows.empty()) {
        ImGui::TextDisabled("No pending dimensions.\nAll caliper values confirmed.");
        return;
    }

    // Table.
    constexpr ImGuiTableFlags kTableFlags =
        ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
        ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_ScrollY;

    const float tableH = std::min((float)d.rows.size() * 28.0f * sc + 28.0f * sc,
                                  240.0f * sc);

    if (ImGui::BeginTable("##dimtable", 5, kTableFlags, ImVec2(0, tableH))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Dimension",   ImGuiTableColumnFlags_WidthStretch, 0.35f);
        ImGui::TableSetupColumn("Value (mm)",  ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableSetupColumn("Source",      ImGuiTableColumnFlags_WidthStretch, 0.20f);
        ImGui::TableSetupColumn("Tier",        ImGuiTableColumnFlags_WidthStretch, 0.15f);
        ImGui::TableSetupColumn("Action",      ImGuiTableColumnFlags_WidthStretch, 0.12f);
        ImGui::TableHeadersRow();

        for (int i = 0; i < (int)d.rows.size(); ++i) {
            DimRow& row = d.rows[i];
            ImGui::PushID(i);
            ImGui::TableNextRow();

            // Col 0 – key
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(row.key.c_str());
            if (row.required) {
                ImGui::SameLine();
                ImGui::TextDisabled("*");
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("fit-critical");
            }

            // Col 1 – value / edit box
            ImGui::TableSetColumnIndex(1);
            if (row.rowState == RowState::Editing) {
                ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
                if (ImGui::InputText("##val", row.editBuf, sizeof(row.editBuf),
                                     ImGuiInputTextFlags_EnterReturnsTrue)) {
                    row.commitEdit();
                }
            } else if (row.rowState == RowState::Accepted) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.4f, 0.85f, 0.4f, 1.0f));
                ImGui::Text("%.4g ✓", row.value_mm);
                ImGui::PopStyleColor();
            } else if (row.rowState == RowState::Pending) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.75f, 0.2f, 1.0f));
                ImGui::Text("%.4g →%.4g", row.value_mm, row.editValue);
                ImGui::PopStyleColor();
            } else {
                ImGui::Text("%.4g", row.value_mm);
            }

            // Col 2 – source
            ImGui::TableSetColumnIndex(2);
            ImGui::TextDisabled("%s", row.source.c_str());

            // Col 3 – tier badge
            ImGui::TableSetColumnIndex(3);
            ImGui::PushStyleColor(ImGuiCol_Text, tierColour(row.tier));
            ImGui::TextUnformatted(row.tier.c_str());
            ImGui::PopStyleColor();

            // Col 4 – action buttons
            ImGui::TableSetColumnIndex(4);
            if (row.rowState == RowState::Editing) {
                if (ImGui::SmallButton("OK"))     row.commitEdit();
                ImGui::SameLine();
                if (ImGui::SmallButton("X"))      row.cancelEdit();
            } else if (row.rowState == RowState::Accepted) {
                ImGui::TextDisabled("done");
            } else {
                if (ImGui::SmallButton("Edit"))   row.beginEdit();
            }

            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    ImGui::Spacing();

    // Submit / status row.
    const bool anyPending = std::any_of(d.rows.begin(), d.rows.end(),
        [](const DimRow& r) { return r.rowState == RowState::Pending; });
    const bool anyEditing = std::any_of(d.rows.begin(), d.rows.end(),
        [](const DimRow& r) { return r.rowState == RowState::Editing; });

    if (d.submitPending) {
        ImGui::TextDisabled("Submitting…");
    } else if (d.submitOk) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 0.85f, 0.4f, 1.0f));
        ImGui::TextUnformatted("Confirmed — gate clear.");
        ImGui::PopStyleColor();
    } else {
        const bool canSubmit = anyPending && !anyEditing;
        if (!canSubmit) ImGui::BeginDisabled();
        if (ImGui::Button("Accept & Submit")) submitAccepted(st);
        if (!canSubmit) ImGui::EndDisabled();
        if (ImGui::IsItemHovered() && !canSubmit)
            ImGui::SetTooltip(anyEditing ? "Finish editing first" : "Edit at least one row");
    }

    if (!d.submitErr.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.3f, 0.2f, 1.0f));
        ImGui::TextWrapped("%s", d.submitErr.c_str());
        ImGui::PopStyleColor();
    }
}

// ─── drawDeviationPanel ───────────────────────────────────────────────────────

void drawDeviationPanel(AppState& st) {
    const float sc = ui::uiScale;

    loadDeviation(st);

    DimReviewState& d = DRS();

    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.accent[0], palette.accent[1],
                                                palette.accent[2], 1.0f));
    ImGui::TextUnformatted("Deviation Heat");
    ImGui::PopStyleColor();
    ImGui::Separator();

    if (d.devPending && !d.devLoaded) {
        ImGui::TextDisabled("Computing deviation…");
        return;
    }

    const DeviationResult& dev = d.deviation;

    if (!d.devLoaded || !dev.ok) {
        if (!dev.reason.empty()) {
            ImGui::TextDisabled("Not available:");
            ImGui::TextWrapped("%s", dev.reason.c_str());
            ImGui::Spacing();
            ImGui::TextDisabled("Export the parametric model to PLY/OBJ,\n"
                                "then call get_reveng_deviation(ref_path=…).");
        } else {
            ImGui::TextDisabled("No deviation data yet.\n"
                                "Run the Model stage and export a reference mesh.");
        }
        return;
    }

    // Max / RMS summary row.
    ImGui::Text("Max:  %.3f mm", dev.max_mm);
    ImGui::SameLine(120.0f * sc);
    ImGui::Text("RMS:  %.3f mm", dev.rms_mm);
    ImGui::Spacing();

    // Pass/warn colouring based on max_mm heuristic (< 0.2 = green, < 0.5 = amber, else red).
    ImVec4 col = (dev.max_mm < 0.2) ? ImVec4(0.3f, 0.85f, 0.4f, 1.0f)
               : (dev.max_mm < 0.5) ? ImVec4(0.95f, 0.65f, 0.10f, 1.0f)
                                     : ImVec4(0.85f, 0.25f, 0.20f, 1.0f);
    const char* verdict = (dev.max_mm < 0.2) ? "Within tight tolerance (< 0.2 mm)"
                        : (dev.max_mm < 0.5) ? "Within loose tolerance (< 0.5 mm)"
                                              : "Exceeds 0.5 mm — review model fit";
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::TextUnformatted(verdict);
    ImGui::PopStyleColor();

    if (dev.buckets.empty()) return;

    ImGui::Spacing();
    ImGui::TextDisabled("Region heat breakdown:");
    ImGui::Spacing();

    // Per-bucket bar chart.
    const float barMaxW = ImGui::GetContentRegionAvail().x - 130.0f * sc;
    for (const auto& b : dev.buckets) {
        ImGui::TextDisabled("%-14s", b.label.c_str());
        ImGui::SameLine(130.0f * sc);
        const float bw = std::max(2.0f, (float)(b.fraction * barMaxW));
        // Heat colour: fraction-weighted amber→red.
        const float heat = (float)std::min(b.fraction * 3.0, 1.0);
        ImVec4 bc(0.3f + 0.6f * heat, 0.7f - 0.4f * heat, 0.2f, 0.85f);
        ImGui::PushStyleColor(ImGuiCol_PlotHistogram, bc);
        char overlay[32];
        std::snprintf(overlay, sizeof(overlay), "%.0f%%", b.fraction * 100.0);
        ImGui::ProgressBar((float)b.fraction, ImVec2(bw, 14.0f * sc), overlay);
        ImGui::PopStyleColor();
    }
}

}  // namespace reveng
}  // namespace ui
