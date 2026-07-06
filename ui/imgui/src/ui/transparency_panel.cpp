// T-U8 Model & telemetry transparency panel — see transparency_panel.h.
//
// Architecture (pattern library §0/§1 — mirrors requirements_panel.cpp):
//   - View-model: aicad::ui::TransparencyModel (ui/abstract/transparency_view.h)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("serving_status") via AsyncRpc (read-only)
//   - Canary opt-in: s.worker.call("serving_set_canary") — synchronous toggle
//   - Consent: currentConsentIndex() / setConsentIndex() from first_run.h
//
// Sections:
//   1. Consent tier  — radio buttons (off/meta/full); disabled when offline forced-off
//   2. Which brain   — per-insertion-point provider/model/adapter table; "not serving"
//      when routes empty or worker unreachable
//   3. Canary opt-in — per-route toggle when canary_pct > 0; shows opt_in_count
//   4. Sidecars      — crash bundle dir (T-Q2) + logging sidecar dir (T-H7)
//   5. Design-Pipeline Action Stream (T-DP11) — live ledger advisory/audit/council/
//      delegation/tool events, each entry inspectable; auto-applied fixes flagged
//
// Erik-ratified rules:
//   - Consent default OFF; offline forces OFF (radio disabled, not hidden).
//   - Canary opt-in default OFF. UI never calls serving_promote / serving_evaluate.
//   - serving_set_canary is only offered per-route when canary_pct > 0.
//   - Style: theme tokens only — no literal ImVec4 / IM_COL32.
#include "transparency_panel.h"
#include "panel_scaffold.h"
#include "transparency_view.h"   // ui/abstract/ (no ImGui includes there)
#include "first_run.h"
#include "settings_modal.h"      // ui::appSettings().offlineMode

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "async_rpc.h"           // core::RpcResult

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

// Platform paths for sidecar dirs
#include "platform/paths.h"

// POSIX filesystem for sidecar scan
#ifndef _WIN32
#  include <dirent.h>
#  include <sys/stat.h>
#endif

namespace ui {

namespace {

// ---- Panel state ------------------------------------------------------------
bool gTransOpen = false;
ui::PanelScaffoldState gTransScaffoldState;
aicad::ui::TransparencyModel gTransModel;

// T-DP11: DP action stream state (lives parallel to existing TransparencyModel)
aicad::ui::DPStreamModel gDPStream;
char gDPRunIdBuf[128] = {};  // editable run_id input field

// ---- Filesystem helpers (POSIX only; Linux-first build) --------------------

static bool dirExists(const std::string& p) {
#ifndef _WIN32
    struct stat st {};
    return stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
#else
    (void)p; return false;
#endif
}

static int countFilesInDir(const std::string& dir) {
#ifndef _WIN32
    DIR* d = opendir(dir.c_str());
    if (!d) return 0;
    int n = 0;
    struct dirent* ent;
    while ((ent = readdir(d)) != nullptr) {
        if (ent->d_name[0] == '.') continue;
        std::string full = dir + "/" + ent->d_name;
        struct stat st {};
        if (stat(full.c_str(), &st) == 0 && S_ISREG(st.st_mode)) ++n;
    }
    closedir(d);
    return n;
#else
    (void)dir; return 0;
#endif
}

// Scan the known sidecar directories and populate model.sidecars.
static void scanSidecars(aicad::ui::TransparencyModel& m) {
    m.sidecars.clear();
    const std::string root = platform::appdataRoot();

    auto addEntry = [&](const std::string& label, const std::string& subpath) {
        aicad::ui::SidecarEntry e;
        e.label  = label;
        e.path   = root + "/" + subpath;
        e.exists = dirExists(e.path);
        if (e.exists) {
            e.file_count = countFilesInDir(e.path);
            e.is_empty   = (e.file_count == 0);
        }
        m.sidecars.push_back(std::move(e));
    };

    addEntry("Crash bundles (T-Q2)",      "crashes");
    addEntry("Telemetry sidecars (T-H7)", "telemetry/sidecars");
    m.sidecars_scanned = true;
}

// ---- JSON parsing -----------------------------------------------------------

static void parseServingStatus(const nlohmann::json& res,
                               aicad::ui::TransparencyModel& m) {
    m.routes.clear();
    m.serving_reachable = res.value("ok", false);

    if (!res.contains("routes") || !res["routes"].is_array()) return;

    for (const auto& r : res["routes"]) {
        aicad::ui::ServingRouteRow row;
        row.insertion_point = r.value("insertion_point", std::string());
        row.provider        = r.value("provider",        std::string());
        row.model           = r.value("model",           std::string());
        row.adapter_version = r.value("adapter_version", std::string());
        row.canary_pct      = (float)r.value("canary_pct", 0.0);
        row.opt_in_count    = r.value("opt_in_count",    0);
        row.is_canary       = (row.canary_pct > 0.f);

        // Check if local machine_id is in the opt_in list (serving_status may
        // include the full list; opt_in_count is always present).
        row.user_opted_in = false;
        if (!m.machine_id.empty() && r.contains("opt_in") &&
                r["opt_in"].is_array()) {
            for (const auto& uid : r["opt_in"]) {
                if (uid.is_string() &&
                        uid.get<std::string>() == m.machine_id) {
                    row.user_opted_in = true;
                    break;
                }
            }
        }
        m.routes.push_back(std::move(row));
    }
}

// ============================================================================
// T-DP11: DP stream event parsing
// ============================================================================

// jstr helper (inline for this TU — does not duplicate the one in mission_control)
static std::string dpjstr(const nlohmann::json& j, const char* k) {
    if (!j.is_object() || !j.contains(k)) return {};
    const auto& v = j[k];
    return v.is_string() ? v.get<std::string>() : std::string{};
}

static aicad::ui::DPStreamKind kindFromStr(const std::string& k) {
    if (k == "advisory")     return aicad::ui::DPStreamKind::Advisory;
    if (k == "audit")        return aicad::ui::DPStreamKind::Audit;
    if (k == "trigger_fired") return aicad::ui::DPStreamKind::TriggerFired;
    if (k == "delegation")   return aicad::ui::DPStreamKind::Delegation;
    if (k == "agent_turn")   return aicad::ui::DPStreamKind::AgentTurn;
    // council artifacts have kind="artifact" with payload.kind="council"
    return aicad::ui::DPStreamKind::Other;
}

// Parse one advisory item from an aicad.advisory/1 items[] entry.
static aicad::ui::DPAdvisoryItem parseAdvisoryItem(const nlohmann::json& it) {
    aicad::ui::DPAdvisoryItem item;
    item.id       = dpjstr(it, "id");
    item.severity = dpjstr(it, "severity");
    item.check    = dpjstr(it, "check");
    item.finding  = dpjstr(it, "finding");
    const auto& src = it.value("source", nlohmann::json::object());
    item.source_kind   = dpjstr(src, "kind");
    item.source_detail = dpjstr(src, "detail");
    if (it.contains("candidate_fixes") && it["candidate_fixes"].is_array()) {
        for (const auto& fx : it["candidate_fixes"]) {
            aicad::ui::DPCandidateFix f;
            f.action           = dpjstr(fx, "action");
            f.basis            = dpjstr(fx, "basis");
            f.predicted_effect = dpjstr(fx, "predicted_effect");
            f.confidence       = dpjstr(fx, "confidence");
            item.candidate_fixes.push_back(std::move(f));
        }
    }
    return item;
}

// Parse a raw ledger event JSON into a DPStreamEntry.
static aicad::ui::DPStreamEntry parseLedgerEvent(const nlohmann::json& ev) {
    aicad::ui::DPStreamEntry e;
    e.seq   = ev.value("seq", 0);
    e.ts    = dpjstr(ev, "ts");
    e.actor = dpjstr(ev, "actor");
    e.kind  = dpjstr(ev, "kind");
    e.display_kind = kindFromStr(e.kind);

    const nlohmann::json& pl = ev.contains("payload") && ev["payload"].is_object()
                               ? ev["payload"] : nlohmann::json::object();

    // Store raw detail JSON for the inspector popover
    try { e.detail_json = pl.dump(2); } catch (...) { e.detail_json = "{}"; }

    // ---- Advisory -----------------------------------------------------------
    if (e.display_kind == aicad::ui::DPStreamKind::Advisory) {
        e.advisory_stage         = dpjstr(pl, "stage");
        e.advisory_user_override = pl.value("user_override", false);
        if (pl.contains("items") && pl["items"].is_array()) {
            for (const auto& it : pl["items"])
                e.advisory_items.push_back(parseAdvisoryItem(it));
        }
        // Summary
        int blockers = 0, fails = 0, warns = 0;
        for (const auto& it : e.advisory_items) {
            if (it.severity == "blocker") ++blockers;
            else if (it.severity == "fail") ++fails;
            else if (it.severity == "warn") ++warns;
        }
        char buf[128];
        if (blockers > 0)
            std::snprintf(buf, sizeof(buf), "%d item(s) — %d blocker(s)",
                          (int)e.advisory_items.size(), blockers);
        else if (fails > 0)
            std::snprintf(buf, sizeof(buf), "%d item(s) — %d fail(s)",
                          (int)e.advisory_items.size(), fails);
        else
            std::snprintf(buf, sizeof(buf), "%d item(s)", (int)e.advisory_items.size());
        e.summary = buf;
    }

    // ---- Audit --------------------------------------------------------------
    else if (e.display_kind == aicad::ui::DPStreamKind::Audit) {
        e.audit_id    = dpjstr(pl, "id");
        e.audit_stage = dpjstr(pl.value("scope", nlohmann::json::object()), "stage");
        e.audit_tokens = pl.value("tokens", 0);
        const auto& mdl = pl.value("model", nlohmann::json::object());
        e.audit_model = dpjstr(mdl, "provider") + ":" + dpjstr(mdl, "id");
        if (pl.contains("verdicts") && pl["verdicts"].is_array()) {
            for (const auto& v : pl["verdicts"]) {
                aicad::ui::DPAuditVerdict vd;
                vd.verdict_class = dpjstr(v, "class");
                vd.severity      = dpjstr(v, "severity");
                vd.finding       = dpjstr(v, "finding");
                const auto& ra = v.value("required_action", nlohmann::json::object());
                vd.required_action_kind = dpjstr(ra, "kind");
                e.audit_verdicts.push_back(std::move(vd));
            }
        }
        // Summary
        int ok = 0, issues = 0;
        for (const auto& vd : e.audit_verdicts)
            (vd.verdict_class == "ok" ? ok : issues)++;
        char buf[128];
        std::snprintf(buf, sizeof(buf), "%d verdict(s): %d ok, %d issue(s)",
                      (int)e.audit_verdicts.size(), ok, issues);
        e.summary = buf;
    }

    // ---- Council (artifact with kind="council") ----------------------------
    else if (e.kind == "artifact" &&
             dpjstr(pl, "kind") == "council") {
        e.display_kind = aicad::ui::DPStreamKind::Council;
        e.council_id   = dpjstr(pl, "id");
        const auto& cr = pl.value("council", nlohmann::json::object());
        e.council_topic = dpjstr(cr, "topic");
        const auto& syn = cr.value("synthesis", nlohmann::json::object());
        e.council_decision = dpjstr(syn, "decision");
        e.council_dissent_count = (int)cr.value("dissents",
                                      nlohmann::json::array()).size();
        char buf[256];
        std::snprintf(buf, sizeof(buf), "%s%s",
                      e.council_topic.substr(0, 60).c_str(),
                      e.council_dissent_count > 0 ? " (has dissents)" : "");
        e.summary = buf;
    }

    // ---- Delegation ---------------------------------------------------------
    else if (e.display_kind == aicad::ui::DPStreamKind::Delegation) {
        e.delegation_node_id = dpjstr(pl, "node_id");
        e.delegation_role    = dpjstr(pl, "role");
        e.delegation_status  = dpjstr(pl, "status");
        e.delegation_depth   = pl.value("depth", 0);
        const auto& bud = pl.value("budget", nlohmann::json::object());
        e.delegation_token_budget = bud.value("token_budget", 0);
        e.delegation_tokens_used  = bud.value("tokens_used", 0);
        e.delegation_wall_budget  = (float)bud.value("wall_budget_s", 0.0);
        e.delegation_elapsed      = (float)bud.value("elapsed_s", 0.0);
        char buf[128];
        std::snprintf(buf, sizeof(buf), "%s → %s (depth %d)",
                      e.delegation_role.c_str(),
                      e.delegation_status.c_str(),
                      e.delegation_depth);
        e.summary = buf;
    }

    // ---- TriggerFired -------------------------------------------------------
    else if (e.display_kind == aicad::ui::DPStreamKind::TriggerFired) {
        e.trigger_id = dpjstr(pl, "trigger_id");
        if (e.trigger_id.empty()) e.trigger_id = dpjstr(pl, "id");
        e.trigger_context_summary = dpjstr(pl, "context_summary");
        e.summary = "trigger: " + e.trigger_id;
    }

    // ---- AgentTurn ----------------------------------------------------------
    else if (e.display_kind == aicad::ui::DPStreamKind::AgentTurn) {
        if (pl.contains("tool_calls") && pl["tool_calls"].is_array()) {
            for (const auto& tc : pl["tool_calls"])
                e.tool_call_names.push_back(dpjstr(tc, "name"));
        }
        char buf[128];
        std::snprintf(buf, sizeof(buf), "%d tool call(s)",
                      (int)e.tool_call_names.size());
        e.summary = buf;
    }

    // ---- Other --------------------------------------------------------------
    else {
        e.summary = e.kind;
    }

    return e;
}

// Merge new events into gDPStream (insert sorted by seq, newest first in display)
static void mergeDPEvents(const nlohmann::json& events_arr) {
    if (!events_arr.is_array()) return;
    for (const auto& ev : events_arr) {
        aicad::ui::DPStreamEntry entry = parseLedgerEvent(ev);
        // Prepend (newest first) — skip if seq already present
        bool dup = false;
        for (const auto& ex : gDPStream.entries)
            if (ex.seq == entry.seq) { dup = true; break; }
        if (!dup) {
            gDPStream.entries.insert(gDPStream.entries.begin(), std::move(entry));
            if (entry.seq > gDPStream.last_seq)
                gDPStream.last_seq = entry.seq;
        }
    }
    // Keep at most 500 entries
    if (gDPStream.entries.size() > 500)
        gDPStream.entries.resize(500);
}

// ---- DP stream RPC ----------------------------------------------------------

// Fire ledger_events RPC for the current run_id, after last_seq.
static void fireDPStreamRpc(app::AppState& s) {
    if (gDPStream.polling || gDPStream.run_id.empty() || !s.workerStarted)
        return;
    gDPStream.polling = true;

    nlohmann::json req;
    req["run_id"]    = gDPStream.run_id;
    req["after_seq"] = gDPStream.last_seq;
    req["kinds"] = nlohmann::json::array({"advisory", "audit", "trigger_fired",
                                          "delegation", "agent_turn", "artifact"});
    req["limit"] = 100;

    s.rpc.call("ledger_events", req, [](const core::RpcResult& r) {
        gDPStream.polling = false;
        if (!r.ok) {
            gDPStream.state    = aicad::ui::DPStreamState::Error;
            gDPStream.errorMsg = r.err;
            return;
        }
        if (!r.result.value("ok", false)) {
            gDPStream.state    = aicad::ui::DPStreamState::Error;
            gDPStream.errorMsg = r.result.value("error", "unknown error");
            return;
        }
        mergeDPEvents(r.result.value("events", nlohmann::json::array()));
        gDPStream.state = aicad::ui::DPStreamState::Live;
    });
}

// ---- Section renderers ------------------------------------------------------

// 1. Consent tier
static void drawConsentSection(app::AppState& s) {
    ImGui::TextColored(TVEC(txt1), "Telemetry Consent");
    ImGui::Separator();
    ImGui::Spacing();

    bool forced_off = gTransModel.offline_mode;

    if (forced_off) {
        ImGui::TextColored(TVEC(txt2),
            "Forced OFF \xe2\x80\x94 offline mode active.");
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("While offline mode is enabled the telemetry "
                              "tier is locked to OFF regardless of this setting.");
        ImGui::Spacing();
    }

    if (forced_off) ImGui::BeginDisabled();

    int consent = ui::currentConsentIndex();
    bool changed = false;
    for (int i = 0; i < 3; ++i) {
        changed |= ImGui::RadioButton(ui::consentLabel(i), &consent, i);
        if (i < 2) ImGui::SameLine();
    }
    if (changed) ui::setConsentIndex(consent);

    if (forced_off) ImGui::EndDisabled();

    ImGui::Spacing();
    ImGui::TextColored(TVEC(txt2),
        "Controls what data AI-CAD may send for improvement. OFF is the default.");
    ImGui::Spacing();
    (void)s;
}

// 2. Which-brain table
static void drawWhichBrainSection() {
    ImGui::TextColored(TVEC(txt1), "Active AI Brain");
    ImGui::Separator();
    ImGui::Spacing();

    if (!gTransModel.serving_reachable && gTransModel.routes.empty()) {
        ImGui::TextColored(TVEC(txt2),
            "Not serving \xe2\x80\x94 worker offline or no routes configured");
        ImGui::Spacing();
        return;
    }

    if (gTransModel.routes.empty()) {
        ImGui::TextColored(TVEC(txt2), "No model routes active");
        ImGui::Spacing();
        return;
    }

    // Table: insertion point | provider | model | adapter | canary%
    const float S = theme::S();
    ImGuiTableFlags tflags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                             ImGuiTableFlags_SizingStretchProp |
                             ImGuiTableFlags_ScrollY;
    float tableH = ImGui::GetTextLineHeightWithSpacing() *
                   (float)(gTransModel.routes.size() + 1.5f);
    if (tableH > 160.f * S) tableH = 160.f * S;

    if (ImGui::BeginTable("##brain_table", 4, tflags, ImVec2(0, tableH))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Endpoint", ImGuiTableColumnFlags_WidthStretch, 0.22f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableSetupColumn("Model",    ImGuiTableColumnFlags_WidthStretch, 0.42f);
        ImGui::TableSetupColumn("Canary",   ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableHeadersRow();

        for (const auto& r : gTransModel.routes) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(r.insertion_point.c_str());
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(r.provider.c_str());
            ImGui::TableSetColumnIndex(2);
            if (r.adapter_version.empty()) {
                ImGui::TextUnformatted(r.model.c_str());
            } else {
                ImGui::Text("%s  [%s]",
                    r.model.c_str(), r.adapter_version.c_str());
            }
            ImGui::TableSetColumnIndex(3);
            if (r.is_canary) {
                ImGui::TextColored(TVEC(warn), "%.0f%%", (double)r.canary_pct);
            } else {
                ImGui::TextColored(TVEC(txt2), "stable");
            }
        }
        ImGui::EndTable();
    }
    ImGui::Spacing();
}

// 3. Canary early-adopter opt-in
static void drawCanarySection(app::AppState& s) {
    // Only render if at least one route has canary_pct > 0
    bool any_canary = false;
    for (const auto& r : gTransModel.routes)
        if (r.is_canary) { any_canary = true; break; }
    if (!any_canary) return;

    ImGui::TextColored(TVEC(txt1), "Early-Adopter Canary Opt-In");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextColored(TVEC(txt2),
        "Opt in per endpoint to receive experimental model builds "
        "ahead of the stable track.");
    ImGui::Spacing();

    for (auto& r : gTransModel.routes) {
        if (!r.is_canary) continue;

        bool opted = r.user_opted_in;
        char label[256];
        std::snprintf(label, sizeof(label),
            "%s  (opt-in count: %d)##canary_%s",
            r.insertion_point.c_str(), r.opt_in_count,
            r.insertion_point.c_str());

        if (ImGui::Checkbox(label, &opted)) {
            // Build the opt_in list: just our machine_id when opting in,
            // empty when opting out. Server replaces the list for our slot.
            nlohmann::json req;
            req["insertion_point"] = r.insertion_point;
            req["pct"]             = (double)r.canary_pct;
            nlohmann::json optInList = nlohmann::json::array();
            if (opted && !gTransModel.machine_id.empty())
                optInList.push_back(gTransModel.machine_id);
            req["opt_in"] = optInList;

            nlohmann::json res;
            std::string err;
            if (s.worker.call("serving_set_canary", req, res, err, 5000)) {
                r.user_opted_in = opted;
                r.opt_in_count  = res.value("opt_in_count", r.opt_in_count);
                gTransScaffoldState.statusText = r.insertion_point +
                    (opted ? ": canary opted in" : ": canary opted out");
                gTransScaffoldState.statusIsError = false;
                std::printf("transparency: serving_set_canary %s opted=%d\n",
                            r.insertion_point.c_str(), (int)opted);
            } else {
                gTransScaffoldState.statusText    = "Canary toggle failed: " + err;
                gTransScaffoldState.statusIsError = true;
                std::printf("transparency: serving_set_canary failed: %s\n",
                            err.c_str());
            }
            std::fflush(stdout);
        }
    }

    ImGui::Spacing();
    ImGui::TextColored(TVEC(warn),
        "Canary builds may have reduced stability. "
        "Auto-promotion requires human sign-off.");
    ImGui::Spacing();
}

// 4. Telemetry sidecar dirs
static void drawSidecarSection() {
    ImGui::TextColored(TVEC(txt1), "Telemetry Sidecars");
    ImGui::Separator();
    ImGui::Spacing();

    if (!gTransModel.sidecars_scanned) {
        ImGui::TextColored(TVEC(txt2), "Scanning...");
        return;
    }

    for (const auto& e : gTransModel.sidecars) {
        ImGui::TextColored(e.exists ? TVEC(txt1) : TVEC(txt2),
                           "%s", e.label.c_str());
        ImGui::SameLine();
        if (!e.exists) {
            ImGui::TextColored(TVEC(txt2), "(not found)");
        } else if (e.is_empty) {
            ImGui::TextColored(TVEC(good), "(empty)");
        } else {
            ImGui::TextColored(TVEC(warn), "(%d file%s)",
                               e.file_count, e.file_count == 1 ? "" : "s");
        }
        ImGui::TextColored(TVEC(txt2), "%s", e.path.c_str());
        ImGui::Spacing();
    }
}

// ============================================================================
// T-DP11: Section 5 — Design-Pipeline Action Stream
// ============================================================================

// Return a color token for advisory/audit severity
static ImVec4 severityColor(const std::string& sev) {
    if (sev == "blocker" || sev == "fail") return TVEC(bad);
    if (sev == "warn")                     return TVEC(warn);
    if (sev == "ok")                       return TVEC(good);
    return TVEC(txt2);  // "info" and unknown
}

// Return a short badge for advisory severity
static const char* severityBadge(const std::string& sev) {
    if (sev == "blocker") return "[BLOCKER]";
    if (sev == "fail")    return "[FAIL]";
    if (sev == "warn")    return "[WARN]";
    if (sev == "ok")      return "[OK]";
    return "[INFO]";
}

// Render the inspector popover content for a single DPStreamEntry.
static void drawEntryInspector(aicad::ui::DPStreamEntry& entry) {
    const float S = theme::S();
    ImGui::PushTextWrapPos(400.f * S);

    // Header row
    ImGui::TextColored(TVEC(txt2), "seq %d  %s  actor: %s",
                       entry.seq, entry.ts.c_str(), entry.actor.c_str());
    ImGui::Spacing();

    // ---- Kind-specific detail -----------------------------------------------
    if (entry.display_kind == aicad::ui::DPStreamKind::Advisory) {
        ImGui::TextColored(TVEC(txt1), "Advisory — stage: %s", entry.advisory_stage.c_str());
        if (entry.advisory_user_override)
            ImGui::TextColored(TVEC(warn), "(user override active — items downgraded to info)");
        ImGui::Spacing();
        for (const auto& it : entry.advisory_items) {
            ImGui::TextColored(severityColor(it.severity), "%s",
                               severityBadge(it.severity));
            ImGui::SameLine();
            ImGui::TextColored(TVEC(txt1), "[%s]", it.check.c_str());
            ImGui::TextColored(TVEC(txt2), "%s", it.finding.c_str());
            if (!it.candidate_fixes.empty()) {
                ImGui::TextColored(TVEC(txt2), "  Fixes (%d):", (int)it.candidate_fixes.size());
                for (const auto& fx : it.candidate_fixes) {
                    ImGui::TextColored(TVEC(accent), "    \xe2\x80\xa2 %s", fx.action.c_str());
                    if (!fx.predicted_effect.empty())
                        ImGui::TextColored(TVEC(txt2), "      effect: %s", fx.predicted_effect.c_str());
                }
            }
            ImGui::Spacing();
        }
    }
    else if (entry.display_kind == aicad::ui::DPStreamKind::Audit) {
        ImGui::TextColored(TVEC(txt1), "Audit %s — stage: %s",
                           entry.audit_id.c_str(), entry.audit_stage.c_str());
        ImGui::TextColored(TVEC(txt2), "model: %s  tokens: %d",
                           entry.audit_model.c_str(), entry.audit_tokens);
        ImGui::Spacing();
        for (const auto& vd : entry.audit_verdicts) {
            ImGui::TextColored(severityColor(vd.severity), "%s",
                               severityBadge(vd.severity));
            ImGui::SameLine();
            ImGui::TextColored(TVEC(txt1), "[%s]", vd.verdict_class.c_str());
            ImGui::TextColored(TVEC(txt2), "%s", vd.finding.c_str());
            if (!vd.required_action_kind.empty() && vd.required_action_kind != "none")
                ImGui::TextColored(TVEC(warn), "  action: %s", vd.required_action_kind.c_str());
            ImGui::Spacing();
        }
    }
    else if (entry.display_kind == aicad::ui::DPStreamKind::Council) {
        ImGui::TextColored(TVEC(txt1), "Council %s", entry.council_id.c_str());
        ImGui::TextColored(TVEC(txt2), "topic: %s", entry.council_topic.c_str());
        ImGui::Spacing();
        if (!entry.council_decision.empty()) {
            ImGui::TextColored(TVEC(accent), "Decision:");
            ImGui::TextColored(TVEC(txt1), "%s", entry.council_decision.c_str());
        }
        if (entry.council_dissent_count > 0)
            ImGui::TextColored(TVEC(warn), "%d dissenting position(s)",
                               entry.council_dissent_count);
    }
    else if (entry.display_kind == aicad::ui::DPStreamKind::Delegation) {
        ImGui::TextColored(TVEC(txt1), "Delegation — %s [%s]",
                           entry.delegation_role.c_str(),
                           entry.delegation_status.c_str());
        ImGui::TextColored(TVEC(txt2), "node: %s  depth: %d",
                           entry.delegation_node_id.c_str(),
                           entry.delegation_depth);
        // Budget meter
        ImGui::Spacing();
        ImGui::TextColored(TVEC(txt2), "Budget:");
        if (entry.delegation_token_budget > 0) {
            float used = (entry.delegation_token_budget > 0)
                ? (float)entry.delegation_tokens_used / (float)entry.delegation_token_budget
                : 0.f;
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%d/%d tokens",
                          entry.delegation_tokens_used,
                          entry.delegation_token_budget);
            ImGui::ProgressBar(used, ImVec2(-1.f, 0.f), buf);
        }
        if (entry.delegation_wall_budget > 0.f) {
            float pct = entry.delegation_elapsed / entry.delegation_wall_budget;
            char buf[64];
            std::snprintf(buf, sizeof(buf), "%.1fs / %.1fs wall",
                          (double)entry.delegation_elapsed,
                          (double)entry.delegation_wall_budget);
            ImGui::ProgressBar(pct, ImVec2(-1.f, 0.f), buf);
        }
    }
    else if (entry.display_kind == aicad::ui::DPStreamKind::TriggerFired) {
        ImGui::TextColored(TVEC(txt1), "Trigger: %s", entry.trigger_id.c_str());
        if (!entry.trigger_context_summary.empty())
            ImGui::TextColored(TVEC(txt2), "%s", entry.trigger_context_summary.c_str());
    }
    else if (entry.display_kind == aicad::ui::DPStreamKind::AgentTurn) {
        ImGui::TextColored(TVEC(txt1), "Agent turn — %d tool call(s)",
                           (int)entry.tool_call_names.size());
        for (const auto& n : entry.tool_call_names)
            ImGui::TextColored(TVEC(accent), "  \xe2\x80\xa2 %s", n.c_str());
    }
    else {
        ImGui::TextColored(TVEC(txt2), "kind: %s", entry.kind.c_str());
    }

    // Raw JSON foldout — always available for deep inspection
    ImGui::Spacing();
    ImGui::Separator();
    if (ImGui::TreeNode("Raw payload")) {
        ImGui::TextColored(TVEC(txt2), "%s", entry.detail_json.c_str());
        ImGui::TreePop();
    }

    ImGui::PopTextWrapPos();
}

// Per-entry row in the stream list
static void drawStreamEntry(aicad::ui::DPStreamEntry& entry, int index) {
    // Severity/kind badge color + label
    ImVec4 rowColor;
    const char* kindBadge;
    const char* kindLabel;

    // Check if any advisory item is an auto-applied fix (confidence=="kernel"
    // candidate_fix applied) — flagged with accent color per DoD.
    bool hasAutoFix = false;
    if (entry.display_kind == aicad::ui::DPStreamKind::Advisory) {
        for (const auto& it : entry.advisory_items)
            for (const auto& fx : it.candidate_fixes)
                if (fx.confidence == "kernel" || fx.confidence == "table")
                    hasAutoFix = true;
    }

    switch (entry.display_kind) {
        case aicad::ui::DPStreamKind::Advisory:
            rowColor  = TVEC(warn);
            kindBadge = "ADV";
            kindLabel = "Advisory";
            break;
        case aicad::ui::DPStreamKind::Audit:
            rowColor  = TVEC(bad);
            kindBadge = "AUD";
            kindLabel = "Audit";
            break;
        case aicad::ui::DPStreamKind::Council:
            rowColor  = TVEC(accent);
            kindBadge = "CON";
            kindLabel = "Council";
            break;
        case aicad::ui::DPStreamKind::Delegation:
            rowColor  = TVEC(txt1);
            kindBadge = "DEL";
            kindLabel = "Delegation";
            break;
        case aicad::ui::DPStreamKind::TriggerFired:
            rowColor  = TVEC(txt2);
            kindBadge = "TRG";
            kindLabel = "Trigger";
            break;
        case aicad::ui::DPStreamKind::AgentTurn:
            rowColor  = TVEC(good);
            kindBadge = "TRN";
            kindLabel = "Agent Turn";
            break;
        default:
            rowColor  = TVEC(txt2);
            kindBadge = "---";
            kindLabel = entry.kind.c_str();
            break;
    }

    // Row: [BADGE] actor — summary            ts  [Inspect]
    char rowId[64];
    std::snprintf(rowId, sizeof(rowId), "##dprow_%d", index);

    ImGui::PushID(index);

    // Badge
    ImGui::TextColored(rowColor, "[%s]", kindBadge);
    ImGui::SameLine();

    // Auto-fix distinct flag (DoD requirement)
    if (hasAutoFix) {
        ImGui::TextColored(TVEC(accent), "[AUTO-FIX]");
        ImGui::SameLine();
    }

    // actor
    ImGui::TextColored(TVEC(txt2), "%s", entry.actor.c_str());
    ImGui::SameLine();

    // summary (dominant part of the row width)
    float avail = ImGui::GetContentRegionAvail().x;
    float tsWidth = 100.f * theme::S();
    float btnWidth = 65.f * theme::S();
    ImGui::SetNextItemWidth(avail - tsWidth - btnWidth - 16.f * theme::S());
    ImGui::TextColored(TVEC(txt1), "%s", entry.summary.c_str());

    // timestamp (right-aligned)
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         ImGui::GetContentRegionAvail().x - tsWidth - btnWidth);
    // Show HH:MM:SS from the ts string (last 8 chars of ISO 8601)
    const char* tsDisp = entry.ts.size() >= 8
        ? entry.ts.c_str() + entry.ts.size() - 8 : entry.ts.c_str();
    ImGui::TextColored(TVEC(txt2), "%s", tsDisp);

    // Inspect button
    ImGui::SameLine();
    if (ImGui::SmallButton("Inspect")) {
        entry.expanded = !entry.expanded;
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show inputs/outputs/why for this %s", kindLabel);

    // Expanded inspector (collapsible below the row)
    if (entry.expanded) {
        ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(accentSoft));
        if (ImGui::BeginChild(rowId, ImVec2(0.f, 0.f), true,
                              ImGuiWindowFlags_AlwaysAutoResize)) {
            drawEntryInspector(entry);
        }
        ImGui::EndChild();
        ImGui::PopStyleColor();
    }

    ImGui::PopID();
}

// 5. Design-Pipeline Action Stream
static void drawDPStreamSection(app::AppState& s) {
    ImGui::TextColored(TVEC(txt1), "Design-Pipeline Action Stream");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextColored(TVEC(txt2),
        "Live stream of helpers, advisories, audits, council votes, "
        "delegations, and tool calls from the active mission ledger.");
    ImGui::Spacing();

    // Run-ID input + Load/Poll controls
    const float S = theme::S();
    ImGui::SetNextItemWidth(240.f * S);
    bool enterPressed = ImGui::InputText("##dp_run_id", gDPRunIdBuf,
                                         sizeof(gDPRunIdBuf),
                                         ImGuiInputTextFlags_EnterReturnsTrue);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Mission run_id (e.g. from mission_start or the DAG panel)");
    ImGui::SameLine();
    bool loadPressed = ImGui::Button("Load") || enterPressed;
    ImGui::SameLine();
    bool pollPressed = ImGui::Button("Poll");
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Fetch new events since last poll");

    if (loadPressed && gDPRunIdBuf[0] != '\0') {
        // Reset stream for new run_id
        gDPStream = aicad::ui::DPStreamModel{};
        gDPStream.run_id   = gDPRunIdBuf;
        gDPStream.state    = aicad::ui::DPStreamState::Loading;
        gDPStream.last_seq = -1;
        fireDPStreamRpc(s);
    } else if (pollPressed && !gDPStream.run_id.empty()) {
        fireDPStreamRpc(s);
    }

    ImGui::Spacing();

    // Status line
    if (gDPStream.state == aicad::ui::DPStreamState::Loading) {
        ImGui::TextColored(TVEC(txt2), "Loading\xe2\x80\xa6");
    } else if (gDPStream.state == aicad::ui::DPStreamState::Error) {
        ImGui::TextColored(TVEC(bad), "Error: %s", gDPStream.errorMsg.c_str());
        ImGui::Spacing();
    } else if (!gDPStream.run_id.empty()) {
        ImGui::TextColored(TVEC(txt2), "run: %s  |  %d event(s)",
                           gDPStream.run_id.c_str(),
                           (int)gDPStream.entries.size());
        ImGui::Spacing();
    }

    // Empty state
    if (gDPStream.state == aicad::ui::DPStreamState::Idle ||
            gDPStream.run_id.empty()) {
        ui::drawPanelEmptyState("No active run",
            "Enter a mission run_id above to load the action stream");
        return;
    }
    if (gDPStream.state == aicad::ui::DPStreamState::Live &&
            gDPStream.entries.empty()) {
        ui::drawPanelEmptyState("Stream empty",
            "No advisory/audit/council/delegation events yet for this run");
        return;
    }
    if (gDPStream.state == aicad::ui::DPStreamState::Loading)
        return;

    // Stream table
    ImGuiTableFlags tflags = ImGuiTableFlags_BordersOuter |
                             ImGuiTableFlags_RowBg |
                             ImGuiTableFlags_ScrollY |
                             ImGuiTableFlags_SizingStretchSame;
    float tableH = std::min(400.f * S,
                            ImGui::GetContentRegionAvail().y - 8.f * S);
    if (tableH < 80.f * S) tableH = 80.f * S;

    if (ImGui::BeginTable("##dp_stream", 1, tflags, ImVec2(0.f, tableH))) {
        ImGui::TableSetupColumn("Event");
        ImGui::TableSetupScrollFreeze(0, 0);

        for (int i = 0; i < (int)gDPStream.entries.size(); ++i) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            drawStreamEntry(gDPStream.entries[i], i);
        }
        ImGui::EndTable();
    }
    ImGui::Spacing();
}

// ---- Main content draw (inside scaffold) ------------------------------------

static void drawContent(app::AppState& s) {
    // Sync offline_mode each frame for coherence
    gTransModel.offline_mode  = ui::appSettings().offlineMode;
    gTransModel.consent_index = ui::currentConsentIndex();

    // Scan sidecar dirs once on first draw (cheap stat calls, no worker needed).
    if (!gTransModel.sidecars_scanned)
        scanSidecars(gTransModel);

    // Trigger serving_status RPC if idle and worker is ready.
    if (gTransModel.state == aicad::ui::TransparencyPanelState::Idle &&
            s.workerStarted) {
        gTransModel.state = aicad::ui::TransparencyPanelState::Loading;
        gTransScaffoldState.statusText    = "Loading\xe2\x80\xa6";
        gTransScaffoldState.statusIsError = false;

        s.rpc.call("serving_status", nlohmann::json::object(),
            [](const core::RpcResult& r) {
                if (!r.ok) {
                    gTransModel.state             = aicad::ui::TransparencyPanelState::Error;
                    gTransModel.errorMsg          = r.err;
                    gTransModel.serving_reachable = false;
                    gTransScaffoldState.statusText    = "Serving status unavailable";
                    gTransScaffoldState.statusIsError = true;
                    return;
                }
                parseServingStatus(r.result, gTransModel);
                gTransModel.state = aicad::ui::TransparencyPanelState::Ready;
                const int nr = (int)gTransModel.routes.size();
                gTransScaffoldState.statusIsError = false;
                gTransScaffoldState.statusText =
                    nr == 0 ? "No active routes"
                            : std::to_string(nr) + " route" +
                              (nr == 1 ? "" : "s") + " active";
            });
    }

    // Loading placeholder
    if (gTransModel.state == aicad::ui::TransparencyPanelState::Loading) {
        // Show consent while loading — it doesn't depend on serving_status
        drawConsentSection(s);
        ImGui::Separator();
        ImGui::Spacing();
        ui::drawPanelEmptyState("Loading\xe2\x80\xa6", "Querying model serving status");
        return;
    }

    // Section 1 — Consent (always present)
    drawConsentSection(s);
    ImGui::Separator();
    ImGui::Spacing();

    // Section 2 — Which brain (draws "not serving" honestly when no routes)
    drawWhichBrainSection();

    // Section 3 — Canary opt-in (hidden when no canary routes)
    if (!gTransModel.routes.empty()) {
        ImGui::Separator();
        ImGui::Spacing();
        drawCanarySection(s);
    }

    // Section 4 — Sidecar dirs
    ImGui::Separator();
    ImGui::Spacing();
    drawSidecarSection();

    // Section 5 — Design-Pipeline Action Stream (T-DP11)
    ImGui::Separator();
    ImGui::Spacing();
    drawDPStreamSection(s);
}

} // anonymous namespace

// ---- Public API -------------------------------------------------------------

void drawTransparencyPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id        = "transparency";
    spec.title     = "Model & Telemetry";
    spec.hasSearch = false;

    ui::drawPanelScaffold(spec, gTransScaffoldState, [&]() {
        drawContent(s);
    });
}

void openTransparencyPanel() {
    gTransOpen = true;
    // Reset so a fresh serving_status RPC fires on next draw.
    gTransModel = aicad::ui::TransparencyModel{};
    gTransScaffoldState = ui::PanelScaffoldState{};
    // Stable per-install identifier (optional; canary toggle no-ops if unset).
    const char* mid = std::getenv("AICAD_MACHINE_ID");
    if (mid && *mid) gTransModel.machine_id = mid;
    // T-DP11: reset the DP stream (run_id stays empty until user enters one or
    // a shot seeds it).
    gDPStream = aicad::ui::DPStreamModel{};
    gDPRunIdBuf[0] = '\0';
}

bool* transparencyPanelOpen() {
    return &gTransOpen;
}

void seedTransparencyPanelForShot(const std::string& serving_status_json) {
    gTransOpen = true;
    gTransModel = aicad::ui::TransparencyModel{};
    gTransScaffoldState = ui::PanelScaffoldState{};

    try {
        nlohmann::json res = nlohmann::json::parse(serving_status_json);
        parseServingStatus(res, gTransModel);
        gTransModel.state             = aicad::ui::TransparencyPanelState::Ready;
        gTransScaffoldState.statusText = std::to_string(gTransModel.routes.size()) +
                                         " route(s) — fixture";
    } catch (...) {
        gTransModel.state    = aicad::ui::TransparencyPanelState::Error;
        gTransModel.errorMsg = "Shot fixture JSON parse error";
        gTransScaffoldState.statusText    = "Parse error";
        gTransScaffoldState.statusIsError = true;
    }

    // Scan real sidecar dirs so screenshot reflects on-disk state.
    scanSidecars(gTransModel);
}

void seedDPStreamForShot(const std::string& events_json,
                         const std::string& run_id) {
    // Reset stream and load fixture events — used by AI_CAD_SHOT=dp_stream
    gTransOpen = true;
    gDPStream  = aicad::ui::DPStreamModel{};
    gDPStream.run_id = run_id.empty() ? "fixture-run-001" : run_id;
    gDPRunIdBuf[0] = '\0';
    std::snprintf(gDPRunIdBuf, sizeof(gDPRunIdBuf), "%s", gDPStream.run_id.c_str());

    try {
        nlohmann::json arr = nlohmann::json::parse(events_json);
        mergeDPEvents(arr);
        gDPStream.state = aicad::ui::DPStreamState::Live;
    } catch (...) {
        gDPStream.state    = aicad::ui::DPStreamState::Error;
        gDPStream.errorMsg = "Shot fixture JSON parse error";
    }

    // Also ensure the T-U8 sections are in Ready state so the panel renders
    // without waiting for a serving_status RPC in headless mode.
    if (gTransModel.state != aicad::ui::TransparencyPanelState::Ready)
        gTransModel.state = aicad::ui::TransparencyPanelState::Ready;
    if (!gTransModel.sidecars_scanned) scanSidecars(gTransModel);
    gTransScaffoldState.statusText = "fixture — " +
                                     std::to_string(gDPStream.entries.size()) +
                                     " event(s)";
}

} // namespace ui
