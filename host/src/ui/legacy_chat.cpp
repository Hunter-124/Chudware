// AI-CAD legacy Design panel (extracted from gui_app.cpp).
//
// Also home to the shared small-UI helpers (primaryButton/sectionHeader/dimText/toggleChip and
// the code/diff/prose/fit/plan/parts draw helpers) declared in core/app_state.h — they live here
// because the Design panel is their primary user, and are shared with the other panels via the
// header.
#include <cstring>
#include <string>

#include "imgui.h"

#include "legacy_chat.h"

#include "../core/app_state.h"
#include "../core/jobs.h"

#include "../worker_client.h"

// ============================ shared small-UI helpers ============================
bool primaryButton(const char* label, const ImVec2& size) {
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.176f, 0.831f, 0.749f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0.31f, 0.90f, 0.82f, 1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(0.10f, 0.69f, 0.61f, 1));
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.04f, 0.10f, 0.10f, 1));
    bool r = ImGui::Button(label, size);
    ImGui::PopStyleColor(4);
    return r;
}
void sectionHeader(ImFont* f, const char* text) {
    if (f) ImGui::PushFont(f);
    ImGui::TextUnformatted(text);
    if (f) ImGui::PopFont();
    ImGui::Dummy(ImVec2(0, 2));
}
void dimText(const char* t) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                                                ui::palette.textDim[2], 1));
    ImGui::TextWrapped("%s", t);
    ImGui::PopStyleColor();
}

// A small pill button that highlights when its section is expanded.
bool toggleChip(const char* label, bool active) {
    if (active) ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(0.176f, 0.831f, 0.749f, 0.30f));
    bool clicked = ImGui::SmallButton(label);
    if (active) ImGui::PopStyleColor();
    return clicked;
}

// Read-only, copyable monospace code view; height fits the content up to maxH.
void drawCodeBox(const char* id, const std::string& code, ImFont* mono, float maxH) {
    int lines = 1;
    for (char c : code) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    if (h < lh + 10.0f) h = lh + 10.0f;
    ImGui::InputTextMultiline(id, (char*)code.c_str(), code.size() + 1, ImVec2(-1, h),
                              ImGuiInputTextFlags_ReadOnly);
    if (mono) ImGui::PopFont();
}

// Colored unified-diff view (+ green / - red / @@ blue) in a scrolling box.
void drawDiffBox(const char* id, const std::string& diff, ImFont* mono, float maxH) {
    int lines = 1;
    for (char c : diff) if (c == '\n') ++lines;
    if (mono) ImGui::PushFont(mono);
    float lh = ImGui::GetTextLineHeightWithSpacing();
    float h = lh * (float)lines + 10.0f;
    if (h > maxH) h = maxH;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.055f, 0.065f, 0.085f, 1));
    ImGui::BeginChild(id, ImVec2(0, h), ImGuiChildFlags_Border,
                      ImGuiWindowFlags_HorizontalScrollbar);
    size_t start = 0;
    while (start <= diff.size()) {
        size_t nl = diff.find('\n', start);
        std::string ln = diff.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
        ImVec4 col(0.78f, 0.82f, 0.88f, 1);
        if (ln.rfind("+++", 0) == 0 || ln.rfind("---", 0) == 0) col = ImVec4(0.60f, 0.66f, 0.74f, 1);
        else if (ln.rfind("@@", 0) == 0) col = ImVec4(0.45f, 0.74f, 0.96f, 1);
        else if (!ln.empty() && ln[0] == '+') col = ImVec4(0.46f, 0.86f, 0.55f, 1);
        else if (!ln.empty() && ln[0] == '-') col = ImVec4(0.96f, 0.50f, 0.50f, 1);
        ImGui::TextColored(col, "%s", ln.empty() ? " " : ln.c_str());
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
    if (mono) ImGui::PopFont();
}

// Dim, wrapped prose (reasoning / notes) in a scrolling box.
void drawProseBox(const char* id, const std::string& text, float maxH) {
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.085f, 0.10f, 0.13f, 1));
    ImGui::BeginChild(id, ImVec2(0, maxH), ImGuiChildFlags_Border);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.70f, 0.74f, 0.80f, 1));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// Fit/interference readout: a green ("fits") or red ("clash") status line; when detailed,
// the per-pair overlap volumes (red) and clearance gaps (dim) below it.
void drawFitReadout(const FitReport& fit, bool detailed) {
    if (!fit.present) return;
    const ImVec4 good(0.46f, 0.86f, 0.55f, 1), bad(0.96f, 0.55f, 0.5f, 1),
        dim(0.62f, 0.66f, 0.72f, 1);
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

// The build-plan card: an accent-edged panel listing each planned part with its purpose,
// committed dimensions (mono) and feature bullets — the agent's blueprint, shown before
// any code runs so the user can see (and steer) what is about to be built.
void drawPlanCard(const PlanInfo& plan, const ui::Fonts& fonts) {
    const ImVec4 accent(ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2], 1);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.071f, 0.102f, 0.110f, 1));
    ImGui::BeginChild("##plancard", ImVec2(0, 0),
                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 w0 = ImGui::GetWindowPos();
    dl->AddRectFilled(w0, ImVec2(w0.x + 3.0f, w0.y + ImGui::GetWindowHeight()),
                      IM_COL32(45, 212, 191, 150));
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
        ImGui::Dummy(ImVec2(0, 2));
        ImGui::ColorButton("##pdot", partColor((int)i),
                           ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                           ImVec2(10, 10));
        ImGui::SameLine(0, 7);
        ImGui::PushFont(fonts.medium);
        ImGui::TextUnformatted(p.name.c_str());
        ImGui::PopFont();
        if (!p.purpose.empty()) dimText(p.purpose.c_str());
        if (!p.dims.empty()) {
            ImGui::PushFont(fonts.mono);
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.64f, 0.72f, 0.78f, 1));
            ImGui::TextWrapped("%s  (mm)", p.dims.c_str());
            ImGui::PopStyleColor();
            ImGui::PopFont();
        }
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.60f, 0.64f, 0.70f, 1));
        for (const auto& f : p.features) {
            ImGui::Bullet();
            ImGui::SameLine();
            ImGui::TextWrapped("%s", f.c_str());
        }
        ImGui::PopStyleColor();
        ImGui::PopID();
    }
    if (!plan.fit.empty()) {
        ImGui::Dummy(ImVec2(0, 2));
        dimText(("Fit: " + plan.fit).c_str());
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// One line "Parts: a, b, c" with each name tinted its assembly color.
void drawPartsLine(const std::vector<PartInfo>& parts) {
    if (parts.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
    ImGui::TextUnformatted("Parts:");
    ImGui::PopStyleColor();
    for (size_t i = 0; i < parts.size(); ++i) {
        ImGui::SameLine();
        std::string lbl = parts[i].name + (i + 1 < parts.size() ? "," : "");
        ImGui::TextColored(partColor((int)i), "%s", lbl.c_str());
    }
}

namespace ui {

// ============================ Design panel ============================
void drawDesignPanel(AppState& st, bool busy, bool canRun) {
    ui::Fonts& fonts = st.fonts;

    // ---------- Design ----------
    if (st.showDesign) {
    if (ImGui::Begin("Design"))
    {
        float panelW = ImGui::GetContentRegionAvail().x;
        if (st.wordmark.id) {
            float w = panelW < 230 ? panelW : 230.0f;
            ImGui::Image((ImTextureID)(intptr_t)st.wordmark.id, ImVec2(w, w * (float)st.wordmark.h / st.wordmark.w));
        } else {
            sectionHeader(fonts.h1, "AI-CAD");
        }
        dimText("Parametric CAD, driven by AI");

        if (!st.workerStarted) {
            ImGui::Dummy(ImVec2(0, 6));
            ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.20f, 0.13f, 0.10f, 1));
            ImGui::BeginChild("setup", ImVec2(0, 132), ImGuiChildFlags_Border);
            if (st.setupRunning.load()) {
                ImGui::TextColored(ImVec4(0.98f, 0.78f, 0.45f, 1), "Setting up runtime...");
                dimText("First run installs an isolated Python + build123d (~once).");
            } else {
                ImGui::TextColored(ImVec4(0.98f, 0.7f, 0.5f, 1), "Runtime not ready");
                dimText("Run the packaged build (dist) or install.ps1 so the engine can "
                        "self-install, then use Repair runtime.");
                if (ImGui::Button("Try setup now")) st.startSetup(false);
            }
            ImGui::EndChild();
            ImGui::PopStyleColor();
        }

        ImGui::Dummy(ImVec2(0, 6));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, 6));

        if (!st.designStarted) {
            // ----- prompt mode: describe a part -----
            sectionHeader(fonts.h2, "Describe a part");
            ImGui::SetNextItemWidth(-1);
            bool promptEnter = ImGui::InputTextMultiline(
                "##prompt", st.prompt.data(), st.prompt.size(),
                ImVec2(-1, ImGui::GetTextLineHeight() * 2.6f),
                ImGuiInputTextFlags_EnterReturnsTrue |
                    ImGuiInputTextFlags_CtrlEnterForNewLine);
            dimText("Enter starts the design - Ctrl+Enter for a new line.");
            ImGui::Dummy(ImVec2(0, 4));
            dimText("Or try one of these:");
            static const char* kExamples[] = {
                "a GoPro tripod mount adapter",
                "a vented Raspberry Pi 5 case with a snap-on lid",
                "an M3 hex standoff, 25mm tall",
            };
            for (int ei = 0; ei < 3; ++ei) {
                if (ImGui::SmallButton(kExamples[ei]))
                    std::strncpy(st.prompt.data(), kExamples[ei], st.prompt.size() - 1);
            }
            ImGui::Dummy(ImVec2(0, 2));
            bool canStart = canRun && st.prompt[0];
            if (!canStart) ImGui::BeginDisabled();
            bool goDesign = primaryButton("Design with AI", ImVec2(-1, 38));
            if (!canStart) ImGui::EndDisabled();
            if ((goDesign || promptEnter) && canStart) {
                std::string p = st.prompt.data();
                st.designStarted = true;        // conversation takes over this section
                { ChatMsg u; u.fromUser = true; u.text = p; st.chat.push_back(u); }
                { ChatMsg a; a.running = true; st.chat.push_back(a); }  // live assistant turn
                st.addLog("[..] designing: " + p);
                st.status = "Designing...";
                st.statusKind = 0;
                launchJob(st.worker, st.job, JOB_DESIGN, p, /*reset=*/true,
                          st.settings.planMode);  // new part = fresh session
            }
        } else {
            // ----- conversation takeover (bounded so the code/build section below stays
            //       visible — the chat never covers it) -----
            sectionHeader(fonts.h2, "Conversation");
            ImGui::SameLine();
            const float nb = 108.0f;
            ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - nb);
            if (busy) ImGui::BeginDisabled();
            if (ImGui::Button("+ New part", ImVec2(nb, 0))) {
                st.designStarted = false;       // back to "Design with AI"
                freeChatTextures(st.chat);
                st.chat.clear();
                { ChatMsg w; w.text = "Describe a part and I'll model it in build123d, "
                                      "render it, and refine it with you."; st.chat.push_back(w); }
                st.modelInfo.clear();
                st.livePhase.clear();
                st.asmParts.clear();
                st.asmFit = FitReport{};
                st.asmStl.clear();
                st.selectedPart = -1;
                st.chatInput[0] = 0;
                st.status = st.workerStarted ? "Ready." : st.status;
                st.statusKind = 0;
            }
            if (busy) ImGui::EndDisabled();

            const float inputH = ImGui::GetFrameHeightWithSpacing() + 8;
            const float buildReserve = 200.0f;  // keep room for the code/build section below
            float chatH = ImGui::GetContentRegionAvail().y - inputH - buildReserve;
            if (chatH < 130.0f) chatH = 130.0f;
            ImGui::BeginChild("chatlog", ImVec2(0, chatH), ImGuiChildFlags_Border);
            const ImVec4 accentCol(ui::palette.accent[0], ui::palette.accent[1],
                                   ui::palette.accent[2], 1);
            const float logW = ImGui::GetContentRegionAvail().x;
            for (size_t mi = 0; mi < st.chat.size(); ++mi) {
                ChatMsg& m = st.chat[mi];
                ImGui::PushID((int)mi);
                if (m.fromUser) {
                    // user turn: a soft accent-tinted bubble
                    ImGui::PushStyleColor(ImGuiCol_ChildBg,
                                          ImVec4(0.176f, 0.831f, 0.749f, 0.07f));
                    ImGui::PushStyleColor(ImGuiCol_Border,
                                          ImVec4(0.176f, 0.831f, 0.749f, 0.22f));
                    ImGui::BeginChild("##umsg", ImVec2(0, 0),
                                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
                    ImGui::PushFont(fonts.medium);
                    ImGui::TextColored(accentCol, "You");
                    ImGui::PopFont();
                    ImGui::TextWrapped("%s", m.text.c_str());
                    ImGui::EndChild();
                    ImGui::PopStyleColor(2);
                    ImGui::Dummy(ImVec2(0, 6));
                    ImGui::PopID();
                    continue;
                }
                ImGui::PushFont(fonts.medium);
                ImGui::TextColored(ImVec4(0.78f, 0.82f, 0.90f, 1), "Assistant");
                ImGui::PopFont();
                if (m.plan.present) drawPlanCard(m.plan, fonts);
                if (!m.text.empty()) {
                    if (m.errKind == ERR_SYSTEM)
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.96f, 0.6f, 0.5f, 1));
                    else if (m.errKind == ERR_CAD)
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.93f, 0.80f, 0.56f, 1));
                    ImGui::TextWrapped("%s", m.text.c_str());
                    if (m.errKind != ERR_NONE) ImGui::PopStyleColor();
                }

                // iteration trace — one expandable card per pass of the agent loop
                for (size_t si = 0; si < m.steps.size(); ++si) {
                    AgentStep& s = m.steps[si];
                    ImGui::PushID((int)si);
                    ImGui::Dummy(ImVec2(0, 2));
                    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0.082f, 0.094f, 0.118f, 1));
                    ImGui::BeginChild("##stepcard", ImVec2(0, 0),
                                      ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY);
                    // Build issues are amber, not red: a failed pass is normal mid-loop —
                    // the error goes straight back to the model, which usually fixes it.
                    ImVec4 badge = s.ok ? (s.done ? accentCol : ImVec4(0.60f, 0.78f, 0.95f, 1))
                                        : ImVec4(0.95f, 0.76f, 0.42f, 1);
                    const char* word = s.ok ? (s.done ? "done" : "built") : "build issue";
                    ImGui::PushFont(fonts.medium);
                    ImGui::TextColored(badge, "Iteration %d - %s", s.index + 1, word);
                    ImGui::PopFont();
                    if (!s.dims.empty()) {
                        ImGui::SameLine();
                        ImGui::PushFont(fonts.mono);
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
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
                        if (!s.triedThumb) {
                            s.thumb = loadTexture(s.contactSheet);
                            s.triedThumb = true;
                        }
                        if (s.thumb.id) {
                            float tw = logW < 220.0f ? logW : 220.0f;
                            float th = tw * (float)s.thumb.h / (float)s.thumb.w;
                            ImGui::Image((ImTextureID)(intptr_t)s.thumb.id, ImVec2(tw, th));
                        }
                    }

                    if (!s.notes.empty()) dimText(s.notes.c_str());
                    if (!s.ok && !s.error.empty()) {
                        // Calm one-line cause; the raw traceback stays behind a toggle so
                        // a self-correcting pass never reads like a crash.
                        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.76f, 0.42f, 1));
                        ImGui::TextWrapped("%s", errSummary(s.error).c_str());
                        ImGui::PopStyleColor();
                        dimText("The CAD code failed this pass; the error was fed back to "
                                "the AI to correct.");
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
                        drawProseBox("##think", s.thinking, 150.0f);
                    if (s.showCode && !s.code.empty())
                        drawCodeBox("##code_v", s.code, fonts.mono, 260.0f);
                    if (s.showDiff && !s.diff.empty())
                        drawDiffBox("##diff_v", s.diff, fonts.mono, 220.0f);
                    if (s.showError && !s.error.empty())
                        drawCodeBox("##err_v", s.error, fonts.mono, 200.0f);
                    ImGui::EndChild();
                    ImGui::PopStyleColor();
                    ImGui::PopID();
                }

                if (m.running) {
                    ImGui::Dummy(ImVec2(0, 2));
                    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.62f, 0.66f, 0.72f, 1));
                    ImGui::TextWrapped("%s", m.liveStatus.empty() ? "working..."
                                                                  : m.liveStatus.c_str());
                    ImGui::PopStyleColor();
                }
                ImGui::Dummy(ImVec2(0, 8));
                ImGui::PopID();
            }
            if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 6) ImGui::SetScrollHereY(1.0f);
            ImGui::EndChild();

            bool send = false;
            ImGui::SetNextItemWidth(-72);
            if (ImGui::InputText("##chatin", st.chatInput.data(), st.chatInput.size(),
                                 ImGuiInputTextFlags_EnterReturnsTrue)) {
                send = true;
                ImGui::SetKeyboardFocusHere(-1);  // keep typing without re-clicking
            }
            ImGui::SameLine();
            if (!canRun) ImGui::BeginDisabled();
            if (ImGui::Button("Send", ImVec2(-1, 0))) send = true;
            if (!canRun) ImGui::EndDisabled();
            if (send && canRun && st.chatInput[0]) {
                std::string msg = st.chatInput.data();
                { ChatMsg u; u.fromUser = true; u.text = msg; st.chat.push_back(u); }
                { ChatMsg a; a.running = true; st.chat.push_back(a); }  // live assistant turn
                st.addLog("[..] chat: " + msg);
                st.status = "Refining...";
                st.statusKind = 0;
                launchJob(st.worker, st.job, JOB_CHAT, msg, /*reset=*/false,
                          st.settings.planMode);  // continue the session
                st.chatInput[0] = 0;
            }
        }

        // ----- build123d code: ALWAYS visible below (in both modes) -----
        ImGui::Dummy(ImVec2(0, 8));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, 6));
        sectionHeader(fonts.h2, "build123d code");
        ImGui::PushFont(fonts.mono);
        float codeH = ImGui::GetContentRegionAvail().y - 46;
        if (codeH < 110) codeH = 110;
        ImGui::InputTextMultiline("##code", st.code.data(), st.code.size(), ImVec2(-1, codeH));
        ImGui::PopFont();
        if (!canRun) ImGui::BeginDisabled();
        if (ImGui::Button("Build code", ImVec2(-1, 34))) {
            st.addLog("[..] building code");
            st.status = "Building...";
            st.statusKind = 0;
            st.buildErrSummary.clear();
            st.buildErrFull.clear();
            launchJob(st.worker, st.job, JOB_BUILD, std::string(st.code.data()), false, false);
        }
        if (!canRun) ImGui::EndDisabled();
        if (!st.buildErrSummary.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.95f, 0.76f, 0.42f, 1));
            ImGui::TextWrapped("Build issue - %s", st.buildErrSummary.c_str());
            ImGui::PopStyleColor();
            if (toggleChip(st.showBuildErr ? "Hide details" : "Details", st.showBuildErr))
                st.showBuildErr = !st.showBuildErr;
            if (st.showBuildErr && !st.buildErrFull.empty())
                drawCodeBox("##builderr", st.buildErrFull, fonts.mono, 180.0f);
        }
    }
    ImGui::End();
    }
}

}  // namespace ui
