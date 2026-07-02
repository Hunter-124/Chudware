// Equations editor — ported from host/src/ui/equations_dialog.cpp (C5).
#include "ui/equations_dialog.h"

#include <cctype>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"

#include "app/app_state.h"
#include "theme/icons.h"
#include "theme/theme.h"
#include "ui/appearance_common.h"

namespace ui {

namespace {
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
// dimensions rebuild) and surfacing the engine error as a toast on failure. Returns op success.
bool applyEqOp(app::AppState& st, const nlohmann::json& op, const char* what) {
    if (!st.docMirror.hasDoc()) {
        c5::toast(ToastLevel::Warn, "No document open.");
        return false;
    }
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (!r.ok) {
        // eq_in_use (delete blocked by a referencing feature) and any other engine error surface as
        // a toast — never crash. The engine puts the human message in error_message.
        std::string msg = r.error_message.empty() ? std::string("operation failed") : r.error_message;
        c5::toast(ToastLevel::Error, std::string(what) + ": " + msg);
        c5::addLog(st, "[err] " + std::string(what) + ": " + msg +
                  (r.error_code.empty() ? "" : " [" + r.error_code + "]"));
        return false;
    }
    if (!r.aicm_path.empty()) { std::string le; c5::loadDocMesh(st, r.aicm_path, le); }
    c5::markActiveTabDirty(st);
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

void drawEquationsDialog(app::AppState& st) {
    if (!showEquationsDialog()) return;
    
    // Per-row expr edit buffers (persist across frames) + the Add-row buffers.
    static std::vector<ExprBuf> s_rows;
    static std::vector<char> s_addName{std::vector<char>(64, 0)};
    static std::vector<char> s_addExpr{std::vector<char>(256, 0)};
    static std::string s_addError;  // inline validation error under the Add row

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + 80 * c5::scale(),
                                   vp->WorkPos.y + 110 * c5::scale()), ImGuiCond_Appearing);
    ImGui::SetNextWindowSize(ImVec2(420 * c5::scale(), 320 * c5::scale()), ImGuiCond_Appearing);

    bool open = showEquationsDialog();
    if (ImGui::Begin("Equations", &open)) {
        // ---- header: icon + title ----
        eqIcon("equations", "[=]", c5::metrics::iconHeader());
        ImGui::SameLine(0, c5::metrics::asmHeaderIconGap());
        ImGui::PushStyleColor(ImGuiCol_Text, c5::txt());
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
            ImGui::PushStyleColor(ImGuiCol_Text, c5::warnCol());
            ImGui::TextWrapped("No document open. Open or create a part first.");
            ImGui::PopStyleColor();
        } else {
            // ---- table of current globals ----
            if (globals.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, c5::txtDim());
                ImGui::TextWrapped("No global variables. Add one below.");
                ImGui::PopStyleColor();
            } else {
                ImGuiTableFlags tf = ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg |
                                     ImGuiTableFlags_SizingStretchProp;
                if (ImGui::BeginTable("##eq_table", 3, tf)) {
                    ImGui::TableSetupColumn("Name", ImGuiTableColumnFlags_WidthFixed,
                                            c5::metrics::pmLabelCol());
                    ImGui::TableSetupColumn("Value / Equation", ImGuiTableColumnFlags_WidthStretch);
                    ImGui::TableSetupColumn("", ImGuiTableColumnFlags_WidthFixed,
                                            c5::metrics::pmOkBtn());
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
                            std::memset(rb->buf.data(), 0, rb->buf.size());
                            std::strncpy(rb->buf.data(), expr.c_str(), rb->buf.size() - 1);
                            rb->lastDoc = expr;
                        }

                        ImGui::TableNextRow();
                        ImGui::PushID(name.c_str());

                        // col 0: name (with the SolidWorks "name" quoting for readability)
                        ImGui::TableSetColumnIndex(0);
                        ImGui::PushStyleColor(ImGuiCol_Text, c5::txt());
                        ImGui::TextUnformatted(("\"" + name + "\"").c_str());
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
                        if (ImGui::Button("X", ImVec2(c5::metrics::pmOkBtn(), 0)))
                            pendingDeleteName = name;
                        if (ImGui::IsItemHovered())
                            ImGui::SetTooltip("Delete \"%s\"", name.c_str());

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
            ImGui::SetNextItemWidth(c5::metrics::pmLabelCol());
            ImGui::InputTextWithHint("##add_name", "name", s_addName.data(), s_addName.size());
            ImGui::SameLine();
            ImGui::SetNextItemWidth(-(c5::metrics::asmBtnW() + c5::metrics::U(2)));
            bool exprEnter = ImGui::InputTextWithHint("##add_expr", "= equation", s_addExpr.data(),
                                                      s_addExpr.size(),
                                                      ImGuiInputTextFlags_EnterReturnsTrue);
            ImGui::SameLine();
            bool addClicked = ImGui::Button("Add", ImVec2(c5::metrics::asmBtnW(), 0));

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
                ImGui::PushStyleColor(ImGuiCol_Text, c5::errCol());
                ImGui::TextWrapped("%s", s_addError.c_str());
                ImGui::PopStyleColor();
            }
        }
    }
    ImGui::End();
    showEquationsDialog() = open;  // close box drives the AppState flag
}
}  // namespace ui
