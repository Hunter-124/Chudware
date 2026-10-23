// Equations / Global Variables editor — implementation. See equations_dialog.h.
//
// Mirrors the AsmPanels mate-form pattern: a Begin()/End() window gated by an AppState bool, the
// ui::metrics::* sizing, palette/pushText use, st.toast() for engine errors, and the applyOps +
// loadDocMesh(aicm) reload so dependent dimensions update after a successful op.
#include "equations_dialog.h"

#include <cctype>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"     // ImRect (ui::adaptivePanel / ui::glassPanel rect argument)

#include "icons.h"
#include "layout.h"             // setNextCommandDialogPlacement (floating dialog anchor)
#include "metrics.h"
#include "widgets/kit.h"        // kit::iconButton (row delete action)

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"

namespace ui {

namespace {

// ---- small theme helpers (mirror asm_panels.cpp) ----
void pushText(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}
void eqIcon(const char* name, const char* fallback, float size) {
    IconUV iv = getIcon(name, 20);
    if (iv.valid) ImGui::Image(iv.texId, ImVec2(size, size), iv.uv0, iv.uv1);
    else ImGui::TextUnformatted(fallback);
}

// Validate a global-variable name against the engine's identifier rule ^[A-Za-z_]\w*$.
bool validName(const char* s) {
    if (!s || !s[0]) return false;
    unsigned char c0 = (unsigned char)s[0];
    if (!(std::isalpha(c0) || c0 == '_')) return false;
    for (const char* p = s + 1; *p; ++p) {
        unsigned char c = (unsigned char)*p;
        if (!(std::isalnum(c) || c == '_')) return false;
    }
    return true;
}

// Apply a single eq_* op, reloading the viewport mesh from the result aicm on success (so dependent
// dimensions rebuild) and surfacing the engine error as a toast on failure. Returns true when the
// op was *queued* (async); toast/log still fire on the drained result.
bool applyEqOp(AppState& st, const nlohmann::json& op, const char* what) {
    if (!st.docMirror.hasDoc()) {
        st.toast(ui::ToastLevel::Warn, "No document open.");
        return false;
    }
    if (st.docLockedByAgent(st.docMirror.docId())) {
        st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
        return false;
    }
    const std::string whatStr = what ? what : "equation";
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, nlohmann::json::array({op}),
        [&st, whatStr](const core::OpsResponse& r, bool applied) {
            --st.rebuildsInFlight;
            if (!applied) return;
            if (!r.ok) {
                std::string msg = r.error_message.empty() ? std::string("operation failed")
                                                          : r.error_message;
                st.toast(ui::ToastLevel::Error, whatStr + ": " + msg);
                st.addLog("[err] " + whatStr + ": " + msg +
                          (r.error_code.empty() ? "" : " [" + r.error_code + "]"));
                return;
            }
            if (!r.aicm_path.empty()) st.loadDocMeshAsync(r.aicm_path);
            st.markActiveTabDirty();
        });
    return true;
}

// A fixed-size edit buffer for one expr field, keyed by variable name so rows keep their in-progress
// text across frames. The buffer is (re)seeded from the doc value whenever the doc value changes and
// the field is not the one being edited.
struct ExprBuf {
    std::string name;
    std::vector<char> buf{std::vector<char>(256, 0)};
    std::string lastDoc;  // doc expr last seen, to detect external changes
};

}  // namespace

void drawEquationsDialog(AppState& st) {
    if (!st.showEquations) return;
    const auto& pal = ui::palette;
    const bool locked = st.docLockedByAgent(st.docMirror.docId());
    const std::string lockReason = locked ? st.agentLockReason(st.docMirror.docId()) : "";

    // Per-row expr edit buffers (persist across frames) + the Add-row buffers.
    static std::vector<ExprBuf> s_rows;
    static std::vector<char> s_addName{std::vector<char>(64, 0)};
    static std::vector<char> s_addExpr{std::vector<char>(256, 0)};
    static std::string s_addError;  // inline validation error under the Add row

    ui::setNextCommandDialogPlacement("Equations",
        ImVec2(ui::metrics::px(460), ui::metrics::px(360)),
        ImVec2(ui::metrics::px(320), ui::metrics::px(220)));

    bool open = st.showEquations;
    // Liquid-glass v2: transparent native WindowBg so the adaptive glassPanel (painted below,
    // once Begin succeeds) is the only fill — ImGui's dark rect must not compete with the frost.
    // The title bar renders DURING Begin (before our glass paint), so give it its own opaque-ish
    // glassBg backing via a scoped TitleBg push (the theme sets it transparent globally); we then
    // frost only the CONTENT region so the title/X stay crisp instead of filmed over.
    // Opaque title backing: a translucent tint let whatever sits behind (ribbon) ghost through.
    const ImVec4 titleTint(pal.glassBg[0], pal.glassBg[1], pal.glassBg[2], 1.0f);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_TitleBg, titleTint);
    ImGui::PushStyleColor(ImGuiCol_TitleBgActive, titleTint);
    ImGui::PushStyleColor(ImGuiCol_TitleBgCollapsed, titleTint);
    const bool eqVis = ImGui::Begin("Equations", &open);
    ImGui::PopStyleColor(3);  // title bar already rendered; WindowBg stays for the frame
    if (eqVis) {
        // Paint the editor's own frosted-glass background over the content region (below title).
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        const float contentTopY =
            ImGui::GetCursorScreenPos().y - ImGui::GetStyle().WindowPadding.y;
        const ImRect winRect(ImVec2(wp.x, contentTopY), ImVec2(wp.x + ws.x, wp.y + ws.y));
        const ui::AdaptiveSurface& surf = ui::adaptivePanel(
            ui::panelId("host_equations_glass"), winRect, ui::GlassGroup::Global);
        ui::glassPanel(ImGui::GetWindowDrawList(), winRect, ui::GlassLevel::Regular, 12.0f, surf);
        // Default ink for the plain readouts tracks the frost's contrast tone; branches that need
        // semantic color (status warn/err) push their own on top of this.
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

        // ---- header: icon + title ----
        eqIcon("equations", "[=]", ui::metrics::iconHeader());
        ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted("Global Variables");
        ImGui::PopStyleColor();
        ImGui::Separator();

        // ---- pull the current globals from the doc tree ----
        std::vector<std::pair<std::string, std::string>> globals;  // (name, expr)
        if (st.docMirror.hasDoc()) {
            const auto& tree = st.docMirror.tree();
            if (tree.is_object()) {
                auto eq = tree.find("equations");
                if (eq != tree.end() && eq->is_object()) {
                    auto gl = eq->find("globals");
                    if (gl != eq->end() && gl->is_array())
                        for (const auto& g : *gl)
                            if (g.is_object())
                                globals.emplace_back(g.value("name", std::string()),
                                                     g.value("expr", std::string()));
                }
            }
        }

        if (!st.docMirror.hasDoc()) {
            pushText(pal.statusWarn);
            ImGui::TextWrapped("No document open. Open or create a part first.");
            ImGui::PopStyleColor();
        } else {
            // ---- table of current globals ----
            if (globals.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextWrapped("No global variables. Add one below.");
                ImGui::PopStyleColor();
            } else {
                ImGuiTableFlags tf = ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg |
                                     ImGuiTableFlags_SizingStretchProp;
                if (ImGui::BeginTable("##eq_table", 3, tf)) {
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthFixed,
                                            ui::metrics::pmLabelCol());
                    ImGui::TableSetupColumn("Value / Equation", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("", ImGuiTableColumnFlags_WidthFixed,
                                            ui::metrics::pmOkBtn());
                    ImGui::TableHeadersRow();

                    // Reconcile the per-row edit buffers with the current set of names.
                    for (const auto& g : globals) {
                        bool found = false;
                        for (auto& rb : s_rows) if (rb.name == g.first) { found = true; break; }
                        if (!found) {
                            ExprBuf rb; rb.name = g.first; rb.lastDoc = g.second;
                            std::strncpy(rb.buf.data(), g.second.c_str(), rb.buf.size() - 1);
                            s_rows.push_back(std::move(rb));
                        }
                    }

                    std::string pendingDeleteName;  // applied after the loop (avoid mutating mid-draw)
                    for (const auto& g : globals) {
                        const std::string& name = g.first;
                        const std::string& expr = g.second;
                        // Locate this row's buffer; reseed it if the doc value changed externally.
                        ExprBuf* rb = nullptr;
                        for (auto& cand : s_rows) if (cand.name == name) { rb = &cand; break; }
                        if (rb && rb->lastDoc != expr) {
                            // A lock must not discard a human's uncommitted expression.
                            if (!locked || rb->lastDoc == rb->buf.data()) {
                                std::memset(rb->buf.data(), 0, rb->buf.size());
                                std::strncpy(rb->buf.data(), expr.c_str(), rb->buf.size() - 1);
                            }
                            rb->lastDoc = expr;
                        }

                        ImGui::TableNextRow();
                        ImGui::PushID(name.c_str());

                        // col 0: name (plain text — the quotes are equation syntax, not a label)
                        ImGui::TableSetColumnIndex(0);
                        ImGui::AlignTextToFramePadding();
                        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
                        ImGui::TextUnformatted(name.c_str());
                        ImGui::PopStyleColor();

                        // col 1: editable expr — commit on Enter
                        ImGui::TableSetColumnIndex(1);
                        ImGui::SetNextItemWidth(-1);
                        if (rb && ImGui::InputText("##expr", rb->buf.data(), rb->buf.size(),
                                                   ImGuiInputTextFlags_EnterReturnsTrue)) {
                            applyEqOp(st, {{"op", "eq_set"}, {"name", name},
                                           {"expr", std::string(rb->buf.data())}},
                                      "Edit equation");
                        }

                        // col 2: delete
                        ImGui::TableSetColumnIndex(2);
                        const std::string delTip = "Delete " + name;
                        if (kit::iconButton("del", "delete",
                                            locked ? lockReason.c_str() : delTip.c_str(), !locked))
                            pendingDeleteName = name;

                        ImGui::PopID();
                    }
                    ImGui::EndTable();

                    if (!pendingDeleteName.empty()) {
                        if (applyEqOp(st, {{"op", "eq_delete"}, {"name", pendingDeleteName}},
                                      "Delete equation")) {
                            // Drop the row's edit buffer so a re-add starts fresh.
                            for (size_t i = 0; i < s_rows.size(); ++i)
                                if (s_rows[i].name == pendingDeleteName) {
                                    s_rows.erase(s_rows.begin() + (long)i); break;
                                }
                        }
                    }
                }
            }

            // ---- Add row ----
            ImGui::Spacing();
            ImGui::SeparatorText("Add Global Variable");
            ImGui::SetNextItemWidth(ui::metrics::pmLabelCol());
            ImGui::InputTextWithHint("##add_name", "name", s_addName.data(), s_addName.size());
            ImGui::SameLine();
            ImGui::SetNextItemWidth(-(ui::metrics::asmBtnW() + ui::metrics::U(2)));
            bool exprEnter = ImGui::InputTextWithHint("##add_expr", "= equation", s_addExpr.data(),
                                                      s_addExpr.size(),
                                                      ImGuiInputTextFlags_EnterReturnsTrue);
            ImGui::SameLine();
            bool addClicked = kit::primaryButton("Add", !locked, lockReason.c_str(),
                                                 ImVec2(ui::metrics::asmBtnW(), 0));

            if (addClicked || exprEnter) {
                if (!validName(s_addName.data())) {
                    s_addError = "Name must start with a letter or _ and contain only letters, "
                                 "digits, or _.";
                } else if (!s_addExpr[0]) {
                    s_addError = "Enter an equation (e.g. 25.4 or width * 2).";
                } else {
                    s_addError.clear();
                    if (applyEqOp(st, {{"op", "eq_set"}, {"name", std::string(s_addName.data())},
                                       {"expr", std::string(s_addExpr.data())}},
                                  "Add equation")) {
                        std::memset(s_addName.data(), 0, s_addName.size());
                        std::memset(s_addExpr.data(), 0, s_addExpr.size());
                    }
                }
            }
            if (!s_addError.empty()) {
                pushText(pal.statusErr);
                ImGui::TextWrapped("%s", s_addError.c_str());
                ImGui::PopStyleColor();
            }
        }
        ImGui::PopStyleColor();  // default text ink
    }
    ImGui::End();
    ImGui::PopStyleColor();  // ImGuiCol_WindowBg
    st.showEquations = open;  // close box drives the AppState flag
}

}  // namespace ui
