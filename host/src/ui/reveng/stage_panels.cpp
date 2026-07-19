// Stage panels for the Reverse Engineering workspace center column (contracts/reveng.md).
// See stage_panels.h for the panel list. All worker calls are ASYNC st.rpc.call with
// GL-thread callbacks — never a sync call in a draw path (known freeze bug).
#include "stage_panels.h"

#include <algorithm>
#include <cstdio>
#include <string>

#include "imgui.h"
#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"
#include "../file_dialog.h"   // saveFileDialog for the export-bundle destination

#include "reveng_internal.h"

namespace ui {
namespace reveng {

// ---- shared helpers ----------------------------------------------------------

// Selected session id, or "" when nothing valid is selected.
static std::string selectedSessionId() {
    const RevEngState& s = S();
    if (s.selected < 0 || s.selected >= (int)s.sessions.size()) return {};
    return s.sessions[s.selected].session_id;
}

// Accent section header (same idiom as dim_review.cpp).
static void stageHeader(const char* title) {
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    ImGui::TextUnformatted(title);
    ImGui::PopStyleColor();
    ImGui::Separator();
}

static void drawStickyError(const std::string& err) {
    if (err.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
    ImGui::TextWrapped("Error: %s", err.c_str());
    ImGui::PopStyleColor();
}

static void drawOkLine(const std::string& msg) {
    if (msg.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 0.85f, 0.4f, 1.0f));
    ImGui::TextWrapped("%s", msg.c_str());
    ImGui::PopStyleColor();
}

// Standard async-RPC result unwrap: returns true when the call succeeded; on failure
// writes a human reason into errOut.
static bool rpcOk(const core::RpcResult& r, const char* tool, std::string& errOut) {
    if (!r.ok) {
        errOut = r.err.empty() ? std::string(tool) + " failed" : r.err;
        return false;
    }
    if (!r.result.is_object()) {
        errOut = std::string(tool) + " returned a malformed response";
        return false;
    }
    if (r.result.contains("ok") && !r.result.value("ok", true)) {
        errOut = r.result.value("reason", std::string(tool) + " failed");
        const std::string hint = r.result.value("hint", std::string());
        if (!hint.empty()) errOut += " — " + hint;
        return false;
    }
    return true;
}

// ---- Scale stage -------------------------------------------------------------

static void kickSetScale(AppState& st) {
    RevEngState& s = S();
    ScaleState& sc = s.scale;
    const std::string sid = selectedSessionId();
    if (sc.pending || sid.empty()) return;
    sc.pending = true;
    sc.err.clear();
    sc.okMsg.clear();

    static const char* kMethods[3] = { "aruco_mat", "two_point", "known_dim" };
    nlohmann::json params = nlohmann::json::object();
    if (sc.method == 0) {
        params["board"] = (sc.board == 1) ? "Letter" : "A4";
    } else if (sc.method == 1) {
        if (sc.imageIdx >= 0 && sc.imageIdx < (int)s.galleryImages.size())
            params["image"] = s.galleryImages[(size_t)sc.imageIdx].path;
        params["p1"] = { sc.p1[0], sc.p1[1] };
        params["p2"] = { sc.p2[0], sc.p2[1] };
        params["distance_mm"] = sc.distMm;
    } else {
        static const char* kSources[4] = { "caliper", "datasheet", "photo_fiducial", "guessed" };
        params["feature"]  = std::string(sc.featureBuf);
        params["value_mm"] = sc.knownValueMm;
        params["source"]   = kSources[std::clamp(sc.knownSource, 0, 3)];
    }

    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["method"]     = kMethods[std::clamp(sc.method, 0, 2)];
    args["params"]     = params;

    st.rpc.call("reveng_set_scale", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ScaleState& sc2 = s2.scale;
        sc2.pending = false;
        if (!rpcOk(r, "reveng_set_scale", sc2.err)) return;
        sc2.err.clear();
        if (r.result.contains("scale") && r.result["scale"].is_object()) {
            const auto& sj = r.result["scale"];
            sc2.mirrored    = true;
            sc2.curMethod   = sj.value("method", std::string());
            sc2.mmPerUnit   = sj.value("mm_per_unit", 0.0);
            sc2.residualPct = sj.value("residual_pct", 0.0);
            sc2.confirmed   = sj.value("confirmed", false);
        }
        sc2.okMsg = sc2.confirmed
            ? "Scale confirmed — reconstruction is unblocked."
            : "Scale recorded (not yet confirmed — reconstruction stays gated).";
    });
}

// reveng_print_scale_mat (shared by the Scale aruco section, Deliver panel, and ribbon).
void kickPrintScaleMat(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    if (d.matPending) return;
    d.matPending = true;
    d.matErr.clear();
    d.matPath.clear();
    nlohmann::json args = nlohmann::json::object();
    args["size"] = (d.matSize == 1) ? "Letter" : "A4";
    st.rpc.call("reveng_print_scale_mat", args, [&st](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.matPending = false;
        if (!rpcOk(r, "reveng_print_scale_mat", d2.matErr)) {
            st.toast(ui::ToastLevel::Error, d2.matErr);
            return;
        }
        d2.matErr.clear();
        d2.matPath = r.result.value("path", std::string());
        if (!d2.matPath.empty())
            st.toast(ui::ToastLevel::Success, "Scale mat written: " + d2.matPath);
        else
            st.toast(ui::ToastLevel::Success, "Scale mat printed");
    });
}

// Scale mat print row (combo + button + status) used by both Scale and Deliver panels.
static void drawScaleMatRow(AppState& st) {
    const float sc = ui::uiScale;
    DeliverState& d = S().deliver;
    ImGui::TextUnformatted("Scale mat (ArUco):");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(90.0f * sc);
    ImGui::Combo("##mat_size", &d.matSize, "A4\0Letter\0");
    ImGui::SameLine();
    if (d.matPending) ImGui::BeginDisabled();
    if (ImGui::Button(d.matPending ? "Printing\xe2\x80\xa6" : "Print scale mat"))
        kickPrintScaleMat(st);
    if (d.matPending) ImGui::EndDisabled();
    if (!d.matPath.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 0.85f, 0.4f, 1.0f));
        ImGui::TextWrapped("Wrote %s", d.matPath.c_str());
        ImGui::PopStyleColor();
    }
    drawStickyError(d.matErr);
}

void drawScalePanel(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();
    ScaleState& state = s.scale;

    stageHeader("Scale");

    // Current session scale mirror (from the get_reveng_session detail poll).
    if (state.mirrored) {
        ImGui::Text("Current: %s", state.curMethod.empty() ? "(none)" : state.curMethod.c_str());
        if (state.mmPerUnit > 0.0) {
            ImGui::SameLine();
            ImGui::TextDisabled("  %.4g mm/unit", state.mmPerUnit);
        }
        if (state.residualPct > 0.0) {
            ImGui::SameLine();
            ImGui::TextDisabled("  residual %.2f%%", state.residualPct);
        }
        ImGui::SameLine();
        if (state.confirmed) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.3f, 0.85f, 0.4f, 1.0f));
            ImGui::TextUnformatted("  confirmed");
            ImGui::PopStyleColor();
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.65f, 0.1f, 1.0f));
            ImGui::TextUnformatted("  unconfirmed");
            ImGui::PopStyleColor();
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::GetStyleColorVec4(ImGuiCol_TextDisabled));
        ImGui::TextWrapped("No scale set yet — reconstruction stays gated until scale is confirmed.");
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    // Method selector.
    static const char* kMethodLabels[3] = { "ArUco Mat", "Two Points", "Known Dim" };
    for (int i = 0; i < 3; ++i) {
        if (i > 0) ImGui::SameLine(0.0f, 4.0f * sc);
        const bool active = (state.method == i);
        if (active)
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.85f));
        if (ImGui::Button(kMethodLabels[i])) state.method = i;
        if (active) ImGui::PopStyleColor();
    }
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    if (state.method == 0) {
        ImGui::TextWrapped(
            "Lay the printed ArUco mat next to the part in the photos, then detect. "
            "The solver reads mm-per-unit off the board.");
        ImGui::Spacing();
        drawScaleMatRow(st);
        ImGui::Spacing();
    } else if (state.method == 1) {
        ImGui::TextWrapped(
            "Pick two points on one photo and type the real distance between them.");
        ImGui::Spacing();
        // Image picker from the session gallery.
        if (s.galleryImages.empty()) {
            ImGui::TextDisabled("No photos in the gallery yet — add some in the Source stage.");
        } else {
            if (state.imageIdx >= (int)s.galleryImages.size()) state.imageIdx = 0;
            const std::string& cur = s.galleryImages[(size_t)state.imageIdx].path;
            ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
            if (ImGui::BeginCombo("##tp_img", cur.c_str())) {
                for (int i = 0; i < (int)s.galleryImages.size(); ++i) {
                    const bool sel = (i == state.imageIdx);
                    if (ImGui::Selectable(s.galleryImages[(size_t)i].path.c_str(), sel))
                        state.imageIdx = i;
                    if (sel) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
            ImGui::SetNextItemWidth(110.0f * sc);
            ImGui::DragFloat2("P1 (px)", state.p1, 1.0f, 0.0f, 10000.0f, "%.0f");
            ImGui::SetNextItemWidth(110.0f * sc);
            ImGui::DragFloat2("P2 (px)", state.p2, 1.0f, 0.0f, 10000.0f, "%.0f");
            ImGui::SetNextItemWidth(110.0f * sc);
            ImGui::InputDouble("Distance (mm)", &state.distMm, 0.0, 0.0, "%.3f");
            if (state.distMm <= 0.0) state.distMm = 0.001;
        }
        ImGui::Spacing();
    } else {
        ImGui::TextWrapped(
            "Type one dimension you trust (caliper or datasheet). Everything else scales from it.");
        ImGui::Spacing();
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x * 0.6f);
        ImGui::InputTextWithHint("##kd_feat", "Feature (e.g. output_shaft.dia)",
                                 state.featureBuf, sizeof(state.featureBuf));
        ImGui::SetNextItemWidth(110.0f * sc);
        ImGui::InputDouble("Value (mm)", &state.knownValueMm, 0.0, 0.0, "%.4g");
        if (state.knownValueMm <= 0.0) state.knownValueMm = 0.001;
        ImGui::SetNextItemWidth(150.0f * sc);
        ImGui::Combo("Source", &state.knownSource,
                     "caliper\0datasheet\0photo_fiducial\0guessed\0");
        ImGui::Spacing();
    }

    // Apply row.
    const bool canApply = !state.pending && !selectedSessionId().empty() &&
                          (state.method != 1 || !s.galleryImages.empty()) &&
                          (state.method != 2 || state.featureBuf[0] != '\0');
    if (!canApply) ImGui::BeginDisabled();
    if (ImGui::Button(state.pending ? "Applying\xe2\x80\xa6" : "Apply scale")) kickSetScale(st);
    if (!canApply) ImGui::EndDisabled();
    if (selectedSessionId().empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("(select a session first)");
    }
    ImGui::Spacing();
    drawOkLine(state.okMsg);
    drawStickyError(state.err);
}

// ---- Model stage -------------------------------------------------------------

// Format the scalar entries of a fits[i].params_mm object for the table.
static std::string summarizeParams(const nlohmann::json& params) {
    if (!params.is_object()) return {};
    std::string out;
    char buf[64];
    for (auto it = params.begin(); it != params.end(); ++it) {
        if (it.value().is_number()) {
            std::snprintf(buf, sizeof(buf), "%s=%.4g",
                          it.key().c_str(), it.value().get<double>());
        } else if (it.value().is_array() && it.key() != "face_ids") {
            std::string joined;
            for (const auto& v : it.value()) {
                if (!v.is_number()) { joined.clear(); break; }
                if (!joined.empty()) joined += ",";
                char nb[24];
                std::snprintf(nb, sizeof(nb), "%.3g", v.get<double>());
                joined += nb;
            }
            if (joined.empty()) continue;
            std::snprintf(buf, sizeof(buf), "%s=[%s]", it.key().c_str(), joined.c_str());
        } else {
            continue;
        }
        if (!out.empty()) out += "  ";
        out += buf;
        if (out.size() > 90) { out += "\xe2\x80\xa6"; break; }
    }
    return out;
}

void kickFitPrimitives(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.fitPending || sid.empty()) return;
    m.fitPending = true;
    m.fitErr.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    st.rpc.call("reveng_fit_primitives", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.fitPending = false;
        m2.fitsLoaded = true;
        if (!rpcOk(r, "reveng_fit_primitives", m2.fitErr)) return;
        m2.fitErr.clear();
        m2.fits.clear();
        if (r.result.contains("fits") && r.result["fits"].is_array()) {
            for (const auto& f : r.result["fits"]) {
                if (!f.is_object()) continue;
                FitEntry e;
                e.kind       = f.value("kind", std::string("?"));
                e.confidence = f.value("confidence", 0.0);
                e.source     = f.value("source", std::string());
                if (f.contains("params_mm")) e.paramsSummary = summarizeParams(f["params_mm"]);
                m2.fits.push_back(std::move(e));
            }
        }
    });
}

void kickBuildParametric(AppState& st, int strategyIdx) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.buildPending || sid.empty()) return;
    m.buildPending = true;
    m.buildErr.clear();
    m.buildRunId.clear();

    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    if (strategyIdx == 1)      args["strategy"] = "mesh_fallback";
    else if (strategyIdx == 2) args["strategy"] = "override_approximate";

    st.rpc.call("reveng_build_parametric", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.buildPending = false;
        if (!rpcOk(r, "reveng_build_parametric", m2.buildErr)) return;
        m2.buildErr.clear();
        m2.buildRunId = r.result.value("run_id", std::string());
        m2.descLoaded = false;  // force a result refresh on next poll
    });
}

// describe_reveng_result — throttled refresh of the build outcome summary.
static void loadModelResult(AppState& st) {
    RevEngState& s = S();
    ModelState& m = s.model;
    const std::string sid = selectedSessionId();
    if (m.descPending || sid.empty()) return;
    if (m.descLoaded) return;
    m.descPending = true;
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    st.rpc.call("describe_reveng_result", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        ModelState& m2 = s2.model;
        m2.descPending = false;
        m2.descLoaded  = true;
        if (!rpcOk(r, "describe_reveng_result", m2.descErr)) return;
        m2.descErr.clear();
        if (!r.result.contains("result") || !r.result["result"].is_object()) return;
        const auto& res = r.result["result"];
        m2.descDocId        = res.value("doc_id", std::string());
        m2.descMatchPct     = res.value("match_pct", 0.0);
        m2.descStrategy     = res.value("strategy", std::string());
        m2.descMeshFallback = res.value("mesh_fallback", false);
    });
}

void drawModelPanel(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();
    ModelState& m = s.model;
    const bool haveSession = !selectedSessionId().empty();

    stageHeader("Model");

    // ---- Fits ----
    ImGui::TextUnformatted("Primitive fits (RANSAC hints for the parametric build)");
    ImGui::Spacing();
    if (!haveSession || m.fitPending) ImGui::BeginDisabled();
    if (ImGui::Button(m.fitPending ? "Fitting\xe2\x80\xa6" : "Fit primitives"))
        kickFitPrimitives(st);
    if (!haveSession || m.fitPending) ImGui::EndDisabled();
    if (!haveSession) {
        ImGui::SameLine();
        ImGui::TextDisabled("(select a session first)");
    }
    if (m.fitPending) {
        ImGui::SameLine();
        ImGui::TextDisabled("running…");
    }
    drawStickyError(m.fitErr);

    if (!m.fits.empty()) {
        ImGui::Spacing();
        constexpr ImGuiTableFlags kFlags =
            ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp;
        if (ImGui::BeginTable("##fit_table", 4, kFlags)) {
            ImGui::TableSetupColumn("Kind",       ImGuiTableColumnFlags_WidthStretch, 0.16f);
            ImGui::TableSetupColumn("Confidence", ImGuiTableColumnFlags_WidthStretch, 0.14f);
            ImGui::TableSetupColumn("Params (mm)",ImGuiTableColumnFlags_WidthStretch, 0.55f);
            ImGui::TableSetupColumn("Source",     ImGuiTableColumnFlags_WidthStretch, 0.15f);
            ImGui::TableHeadersRow();
            for (const FitEntry& f : m.fits) {
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted(f.kind.c_str());
                ImGui::TableSetColumnIndex(1);
                const ImVec4 cc = (f.confidence >= 0.8f)
                    ? ImVec4(0.3f, 0.85f, 0.4f, 1.0f)
                    : (f.confidence >= 0.5f ? ImVec4(0.95f, 0.65f, 0.1f, 1.0f)
                                            : ImVec4(0.9f, 0.35f, 0.3f, 1.0f));
                ImGui::PushStyleColor(ImGuiCol_Text, cc);
                ImGui::Text("%.0f%%", f.confidence * 100.0);
                ImGui::PopStyleColor();
                ImGui::TableSetColumnIndex(2);
                ImGui::TextUnformatted(f.paramsSummary.empty() ? "-" : f.paramsSummary.c_str());
                ImGui::TableSetColumnIndex(3);
                ImGui::TextDisabled("%s", f.source.c_str());
            }
            ImGui::EndTable();
        }
    } else if (m.fitsLoaded && m.fitErr.empty()) {
        ImGui::TextDisabled("No primitives fitted yet — run reconstruction first if the mesh is missing.");
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    // ---- Build ----
    ImGui::TextUnformatted("Parametric build");
    ImGui::Spacing();
    ImGui::SetNextItemWidth(220.0f * sc);
    ImGui::Combo("Strategy", &m.strategy,
                 "Auto (fits + AI features)\0Mesh-body fallback\0Override approximate gate\0");
    if (m.strategy == 2) {
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.6f, 0.2f, 1.0f));
        ImGui::TextWrapped("Approximate sources are reference/preview only — overriding "
                           "drops the honesty gate. The result stays tagged approximate.");
        ImGui::PopStyleColor();
    }
    if (!haveSession || m.buildPending) ImGui::BeginDisabled();
    if (ImGui::Button(m.buildPending ? "Launching\xe2\x80\xa6" : "Build parametric model"))
        kickBuildParametric(st, m.strategy);
    if (!haveSession || m.buildPending) ImGui::EndDisabled();
    if (!m.buildRunId.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("run %s", m.buildRunId.c_str());
    }
    drawStickyError(m.buildErr);

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    // ---- Result ----
    loadModelResult(st);
    ImGui::TextUnformatted("Result");
    ImGui::SameLine();
    if (ImGui::SmallButton("Refresh##model_result")) m.descLoaded = false;
    if (m.descPending && !m.descLoaded) {
        ImGui::TextDisabled("Loading…");
    } else if (!m.descErr.empty()) {
        ImGui::TextDisabled("No result yet:");
        ImGui::SameLine();
        ImGui::TextWrapped("%s", m.descErr.c_str());
    } else if (m.descLoaded && !m.descDocId.empty()) {
        ImGui::Text("Doc: %s", m.descDocId.c_str());
        if (m.descMatchPct > 0.0) {
            ImGui::SameLine();
            ImGui::TextDisabled("  match %.0f%%", m.descMatchPct);
        }
        if (!m.descStrategy.empty()) {
            ImGui::SameLine();
            ImGui::TextDisabled("  via %s", m.descStrategy.c_str());
        }
        if (m.descMeshFallback) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.65f, 0.1f, 1.0f));
            ImGui::TextUnformatted("mesh-body fallback (not editable parametric)");
            ImGui::PopStyleColor();
        }
    } else if (m.descLoaded) {
        ImGui::TextDisabled("No result yet — run a build.");
    }
}

// ---- Deliver stage -----------------------------------------------------------

static void kickAttachResult(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    const std::string sid = selectedSessionId();
    if (d.attachPending || sid.empty() || d.partRefBuf[0] == '\0') return;
    d.attachPending = true;
    d.attachErr.clear();
    d.attachOk.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["part_ref"]   = std::string(d.partRefBuf);
    st.rpc.call("reveng_attach_result", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.attachPending = false;
        if (!rpcOk(r, "reveng_attach_result", d2.attachErr)) return;
        d2.attachErr.clear();
        const std::string geo = r.result.value("geometry_ref", std::string());
        d2.attachOk = "Attached to parts hub" +
                      (geo.empty() ? std::string(".") : (" — " + geo));
    });
}

static void kickExportBundle(AppState& st) {
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    const std::string sid = selectedSessionId();
    if (d.exportPending || sid.empty() || d.exportDestBuf[0] == '\0') return;
    d.exportPending = true;
    d.exportErr.clear();
    d.exportOk.clear();
    nlohmann::json args = nlohmann::json::object();
    args["session_id"] = sid;
    args["dest"]       = std::string(d.exportDestBuf);
    st.rpc.call("export_reveng_bundle", args, [](const core::RpcResult& r) {
        RevEngState& s2 = S();
        DeliverState& d2 = s2.deliver;
        d2.exportPending = false;
        if (!rpcOk(r, "export_reveng_bundle", d2.exportErr)) return;
        d2.exportErr.clear();
        const std::string path = r.result.value("path", std::string());
        d2.exportOk = path.empty() ? "Bundle exported." : ("Wrote " + path);
    });
}

void drawDeliverPanel(AppState& st) {
    const float sc = ui::uiScale;
    RevEngState& s = S();
    DeliverState& d = s.deliver;
    const bool haveSession = !selectedSessionId().empty();

    stageHeader("Deliver");

    // ---- Attach to parts hub ----
    ImGui::TextUnformatted("Attach to Parts Hub");
    ImGui::Spacing();
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - 90.0f * sc);
    ImGui::InputTextWithHint("##part_ref", "part_ref (e.g. catalog:grabcad:bracket-123)",
                             d.partRefBuf, sizeof(d.partRefBuf));
    ImGui::SameLine();
    const bool canAttach = haveSession && !d.attachPending && d.partRefBuf[0] != '\0';
    if (!canAttach) ImGui::BeginDisabled();
    if (ImGui::Button(d.attachPending ? "Attaching\xe2\x80\xa6" : "Attach"))
        kickAttachResult(st);
    if (!canAttach) ImGui::EndDisabled();
    drawOkLine(d.attachOk);
    drawStickyError(d.attachErr);

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    // ---- Export bundle ----
    ImGui::TextUnformatted("Export bundle (session + artifacts)");
    ImGui::Spacing();
    ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x - 90.0f * sc);
    ImGui::InputTextWithHint("##export_dest", "destination path (.zip)",
                             d.exportDestBuf, sizeof(d.exportDestBuf));
    ImGui::SameLine();
    if (ImGui::Button("Browse\xe2\x80\xa6##export")) {
        std::string path = ui::saveFileDialog(
            "RE bundle (*.zip)\0*.zip\0All files (*.*)\0*.*\0", "zip", "reveng_bundle.zip");
        if (!path.empty()) {
            std::snprintf(d.exportDestBuf, sizeof(d.exportDestBuf), "%s", path.c_str());
        }
    }
    const bool canExport = haveSession && !d.exportPending && d.exportDestBuf[0] != '\0';
    if (!canExport) ImGui::BeginDisabled();
    if (ImGui::Button(d.exportPending ? "Exporting\xe2\x80\xa6" : "Export bundle"))
        kickExportBundle(st);
    if (!canExport) ImGui::EndDisabled();
    drawOkLine(d.exportOk);
    drawStickyError(d.exportErr);

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();

    // ---- Print scale mat ----
    ImGui::TextUnformatted("Scale mat");
    ImGui::Spacing();
    drawScaleMatRow(st);

    if (!haveSession) {
        ImGui::Spacing();
        ImGui::TextDisabled("(select a session to attach or export)");
    }
}

}  // namespace reveng
}  // namespace ui
