// SimManager study list: the Simulation pane body (takeover routing, study rows), the ONE panel
// header and the grouped New Study picker (Simulation UI standard §2, §6).
#include "sim_manager_internal.h"

#include "../../core/sim_client.h"
#include "../../renderer.h"
#include "../layout.h" // ui::overlayColumnEdge — the study picker flies out beside the column

namespace ui {

namespace {

// One study's status in the kit vocabulary: the row's trailing glyph and the header chip.
struct StudyStatusView {
  kit::Status status = kit::Status::Pending;
  const char *chip = nullptr; // header status chip ("Running", "Solved", ...); null = none
  kit::Tone tone = kit::Tone::Neutral;
  std::string tip;
};

StudyStatusView studyStatusView(const Study &s, bool running, const std::string &setupIssue) {
  StudyStatusView v;
  if (running) {
    v.status = kit::Status::Running;
    v.chip = "Running";
    v.tone = kit::Tone::Accent;
    v.tip = "A run of this study is in progress.";
    return v;
  }
  if (s.type == StudyType::Topology) {
    const bool hasRuns = s.topo.contains("runs") && s.topo["runs"].is_array() &&
                         !s.topo["runs"].empty();
    if (s.topoStatusKind == 2 && !s.topoStatus.empty()) {
      v.status = kit::Status::Failed;
      v.chip = "Failed";
      v.tone = kit::Tone::Err;
      v.tip = s.topoStatus;
    } else if (hasRuns) {
      v.status = kit::Status::Done;
      v.chip = "Solved";
      v.tone = kit::Tone::Ok;
      v.tip = "Retained runs are listed under Results.";
    } else if (!setupIssue.empty()) {
      v.status = kit::Status::Retry;
      v.tip = setupIssue;
    }
    return v;
  }
  if (s.hasResult || s.motionHasResult) {
    v.status = kit::Status::Done;
    v.chip = "Solved";
    v.tone = kit::Tone::Ok;
    v.tip = "Results are ready.";
    return v;
  }
  const bool cancelled = s.summary.find("cancelled") != std::string::npos;
  const bool failed = s.summary.find("failed") != std::string::npos ||
                      s.summary.rfind("Contact:", 0) == 0;
  if (cancelled) {
    v.status = kit::Status::Retry;
    v.chip = "Cancelled";
    v.tone = kit::Tone::Warn;
    v.tip = s.summary;
  } else if (failed) {
    v.status = kit::Status::Failed;
    v.chip = "Failed";
    v.tone = kit::Tone::Err;
    v.tip = s.summary;
  } else if (!setupIssue.empty()) {
    v.status = kit::Status::Retry;
    v.tip = setupIssue;
  }
  return v;
}

// A status mark at `center` with a hover tooltip; Pending draws nothing (an unrun study needs no
// mark of its own).
void statusMark(ImVec2 center, float size, kit::Status s, const std::string &tip) {
  if (s == kit::Status::Pending)
    return;
  kit::statusGlyph(ImGui::GetWindowDrawList(), center, size * 0.5f, s, simSurf());
  const ImVec2 h(size * 0.5f, size * 0.5f);
  if (!tip.empty() &&
      ImGui::IsMouseHoveringRect(ImVec2(center.x - h.x, center.y - h.y),
                                 ImVec2(center.x + h.x, center.y + h.y)) &&
      ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem))
    kit::tooltip(tip.c_str());
}

} // namespace

void SimManager::openNewStudyPicker() {
  // The picker lives on the study list: whatever panel holds the pane yields to it.
  panelOpen_ = false;
  armedWell_ = -1;
  emPanelOpen_ = false;
  elPanelOpen_ = false;
  fwPanelOpen_ = false;
  handoffPanelOpen_ = false;
  piping_.close();
  hydraulic_.close();
  newPickerRequested_ = true;
}

void SimManager::drawTreeBody(AppState &st) {
  appSt_ = &st;
  // Keep the mirror in step with the active document (cheap: re-pull only on a
  // doc/revision change). A failed pull for this exact doc/revision is not re-issued every frame
  // (each attempt is a blocking worker round-trip); the list below shows it with Retry instead.
  const bool listFailedHere = !listErrorDocId_.empty() && st.docMirror.hasDoc() &&
                              listErrorDocId_ == st.docMirror.docId() &&
                              listErrorRevision_ == (long)st.docMirror.revision();
  if (!st.rpc.busy() && st.docMirror.hasDoc() && !listFailedHere &&
      (lastDocId_ != st.docMirror.docId() || lastRevision_ != (long)st.docMirror.revision()))
    refresh(st);

  // The Simulation body is embedded in the FeatureManager column, whose glass surface layout.cpp
  // already painted; its ADAPTIVE text is the pane-level ink for the whole body (mirrors
  // feature_tree.cpp). Only pushed over a live backdrop; on chrome the theme Text role already
  // reads on glassBg.
  const bool paneInk = ui::viewportBackdrop().valid();
  if (paneInk)
    ImGui::PushStyleColor(ImGuiCol_Text, simSurf().textPrimary);
  struct PaneInkPop {
    bool on;
    ~PaneInkPop() {
      if (on)
        ImGui::PopStyleColor();
    }
  } paneInkPop{paneInk};

  // Bind wrapping to this child's live content width (ImGui's default wrap edge can point at the
  // parent window), so prose stays inside the rail at narrow widths.
  ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
  struct WrapPop {
    ~WrapPop() { ImGui::PopTextWrapPos(); }
  } wrapPop;

  // ---- takeover panels: each owns the whole pane while open (header + body + footer) ----
  if (emPanelOpen_) {
    drawEmPanel(st);
    return;
  }
  if (elPanelOpen_) {
    drawElectronicsPanel(st);
    return;
  }
  if (fwPanelOpen_) {
    drawEmulationPanel(st);
    return;
  }
  if (handoffPanelOpen_) {
    drawHandoffPanel(st);
    return;
  }
  // Hydraulics panel; "Open in Piping panel" hands the generated network to the Piping panel.
  if (hydraulic_.isOpen()) {
    hydraulic_.draw(st);
    HydraulicPanel::PipingOpen req;
    if (hydraulic_.takePipingOpen(req)) {
      hydraulic_.close();
      piping_.openHandoff(st, req.pipingDoc, req.boundary, req.fluid, req.solve);
    }
    return;
  }
  if (piping_.isOpen()) {
    piping_.draw(st);
    return;
  }
  // Study editor (opened by a ribbon tool, a picker pick or a setup row). drawForm owns
  // the whole scaffold, including drawPanelHeader and closing on Done.
  if (panelOpen_ && sel_ >= 0 && sel_ < (int)studies_.size()) {
    drawForm(st, studies_[sel_]);
    return;
  }

  // ---- study list ----
  drawNewStudyPicker(st);

  // Cross-sim handoffs, disclosed by the open doc types; above the list so a board-only session
  // still reaches them.
  int pcbTab = -1;
  for (int i = 0; i < (int)st.docTabs.size(); ++i)
    if (st.docTabs[i].kind == AppState::DocTab::TabKind::PCB && !st.docTabs[i].ecadDocId.empty()) {
      pcbTab = i;
      break;
    }
  bool firstHeader = true;
  if (pcbTab >= 0) {
    kit::sectionHeader("Cross-sim handoffs", nullptr, nullptr, -1, true);
    firstHeader = false;
    kit::RowStyle rs;
    rs.icon = "study_thermal_fea";
    rs.secondary = "Board heat sources into a conduction study";
    rs.tooltip = "Fuses the routed board into one FEA solid, applies per-component heat sources "
                 "from the board's power metadata plus environment convection, and solves the "
                 "conduction study.";
    if (kit::row("handoff_thermal", "Send board to thermal FEA", rs))
      openHandoffPanel(st, pcbTab);
  }

  if (!st.docMirror.hasDoc()) {
    if (kit::emptyState("study_static", "No part or assembly open",
                        "Studies live inside a part or assembly document. Schematics, PCBs and "
                        "firmware projects unlock circuit simulation and firmware emulation.",
                        "+ New study"))
      newPickerRequested_ = true;
    return;
  }
  // The study list is answered per document: until this document's sim_study_list lands, the
  // rail says so (loading, or the failure with Retry) — never "No studies yet" for a document that
  // may carry saved studies, and never a blank body.
  if (listFailedHere) {
    const bool listedBefore = lastDocId_ == st.docMirror.docId();
    if (!listedBefore) {
      if (kit::blockedState("Saved studies couldn't be loaded",
                            "The engine didn't return this document's study list.",
                            "Retry. If it keeps failing, see the Console for the engine message.",
                            "Retry"))
        listErrorDocId_.clear();
      return;
    }
    kit::inlineStatus("Showing the last loaded studies; the latest list couldn't be loaded.",
                      kit::Tone::Warn);
    if (kit::secondaryButton("Retry###sim_list_retry"))
      listErrorDocId_.clear();
  } else if (lastDocId_ != st.docMirror.docId()) {
    kit::spinner("Loading saved studies\xe2\x80\xa6");
    return;
  }
  const int industrial = piping_.studyCount() + hydraulic_.studyCount();
  if (studies_.empty() && industrial == 0) {
    if (activeDocIsAssembly(st)) {
      // Say plainly that THIS document has no study: rendered motion videos are files (often of
      // another assembly), never studies of this one.
      const std::string title = "No studies in " + st.docTabs[st.activeTab].title;
      if (kit::emptyState("rotate_component", title.c_str(),
                          "A Motion study drives this assembly's mates and plays the sweep in the "
                          "viewport. Rendered motion videos (.mp4 / .gif) are files, not studies.",
                          "+ New study"))
        newPickerRequested_ = true;
      return;
    }
    if (kit::emptyState("study_static", "No studies yet",
                        "Studies are saved inside this document. Pick a kind, set up material, "
                        "fixtures and loads, then run it.",
                        "+ New study"))
      newPickerRequested_ = true;
    return;
  }

  if (kit::secondaryButton("+ New study###sim_new_study", true, nullptr,
                           ImVec2(ImGui::GetContentRegionAvail().x, 0.0f)))
    newPickerRequested_ = true;
  kit::itemTooltip("Add a simulation study. The kinds offered follow what you have open.");

  if (!studies_.empty()) {
    kit::sectionHeader("Studies", nullptr, nullptr, (int)studies_.size(), false);
    for (int i = 0; i < (int)studies_.size(); ++i)
      drawStudyRow(st, i);
  }
  // Saved piping networks, internal-flow runs, vessels and duct systems (Piping panel) and
  // hydraulic circuits (Hydraulics panel), under one group.
  if (industrial > 0) {
    kit::sectionHeader("Piping, hydraulics & industrial", "study_piping", nullptr, industrial,
                       firstHeader && studies_.empty());
    piping_.drawStudyRows(st);
    hydraulic_.drawStudyRows(st);
  }
}

// ---- grouped "New study" picker ------------------------------------------------------------------
void SimManager::drawNewStudyPicker(AppState &st) {
  if (newPickerRequested_ || shotForcePicker) {
    ImGui::OpenPopup("##sim_new_study");
    newPickerRequested_ = false;
  }
  // Fly out to the RIGHT of the manager column, top-aligned with the pane (the overlay band, never
  // over the ribbon or the list it was opened from) and one gutter above the status bar; the
  // picker caps itself to that height.
  const float colEdge = ui::overlayColumnEdge(0);
  const ImGuiViewport *vp = ImGui::GetMainViewport();
  const ui::OverlayBand &band = ui::overlayBand();
  const float topY = band.valid ? band.minY : std::max(ImGui::GetWindowPos().y, vp->WorkPos.y);
  const float bottomY = band.valid ? band.maxY : vp->WorkPos.y + vp->WorkSize.y;
  const float anchorY = topY + kit::gutter();
  if (colEdge > 0.0f)
    ImGui::SetNextWindowPos(ImVec2(colEdge + kit::gutter(), anchorY), ImGuiCond_Appearing);
  const float pickerMaxH =
      std::max(bottomY - anchorY - kit::gutter(), ImGui::GetFrameHeight() * 4.0f);
  ui::pushGlassWindowStyle();
  ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, kit::rowRound());
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(kit::padX(), kit::padY()));
  if (!ImGui::BeginPopup("##sim_new_study")) {
    ImGui::PopStyleVar(2);
    ui::popGlassWindowStyle();
    return;
  }

  // Progressive disclosure by the open doc types: mechanical kinds need a part/assembly, Motion an
  // assembly, Electronics a schematic, emulation a board or firmware project. Kinds whose engine
  // leg is missing stay listed, dimmed, with the reason.
  bool hasSch = false, hasPcb = false, hasAsm = false;
  for (const auto &t : st.docTabs) {
    hasSch = hasSch || t.kind == AppState::DocTab::TabKind::Schematic;
    hasPcb = hasPcb || t.kind == AppState::DocTab::TabKind::PCB;
    hasAsm = hasAsm || t.kind == AppState::DocTab::TabKind::Assembly;
  }
  const bool mechDoc = st.docMirror.hasDoc();
  const nlohmann::json flowCaps = st.simClient ? st.simClient->caps() : nlohmann::json::object();
  const bool openfoamMissing = !flowCaps.is_object() || !flowCaps.value("openfoam", false);
  auto capHint = [&](const char *leg, const char *flag, const char *fallback,
                     bool &missing) -> std::string {
    const auto it = caps_.find(leg);
    missing = capsStatus_ == 1 && (it == caps_.end() || !it->is_object() || !it->value(flag, false));
    return it != caps_.end() && it->is_object() ? it->value("hint", std::string(fallback))
                                                : std::string(fallback);
  };
  bool ccxMissing = false, vibMissing = false;
  const std::string ccxHint = capHint("ccx", "found", "CalculiX (ccx) not found.", ccxMissing);
  const std::string vibHint = capHint("freq_response", "available",
                                      "Vibration post-processing is unavailable.", vibMissing);
#ifdef __EMSCRIPTEN__
  const bool webNoPy = true;
#else
  const bool webNoPy = false;
#endif
  const std::string webHint = "Not available in the web version yet.";

  enum class Act { Command, Electronics, Firmware };
  struct Entry {
    int group;
    const char *icon, *title, *desc;
    bool enabled;
    std::string reason;
    Act act;
    const char *cmd;
  };
  enum { GStruct, GThermal, GFluids, GMotion, GEm, GElec, GOpt, GIndustrial, GCount };
  static const char *const kGroupTitles[GCount] = {
      "Structural", "Thermal", "Fluids", "Motion", "Electromagnetics", "Electronics",
      "Optimization", "Industrial"};
  std::vector<Entry> entries;
  auto add = [&](int g, const char *icon, const char *title, const char *desc, bool disabled,
                 std::string reason, Act act, const char *cmd) {
    entries.push_back({g, icon, title, desc, !disabled, disabled ? std::move(reason) : std::string(),
                       act, cmd});
  };
  if (mechDoc) {
    add(GStruct, "study_static", "Static", "Stress, displacement, safety factor.", false, {},
        Act::Command, "sim_new_static");
    add(GStruct, "study_modal", "Modal", "Natural frequencies and mode shapes.", false, {},
        Act::Command, "sim_new_modal");
    add(GStruct, "study_buckling", "Buckling", "Buckling load factors and shapes.", false, {},
        Act::Command, "sim_new_buckling");
    add(GStruct, "study_fatigue", "Fatigue", "Cyclic-load life and safety factor.", false, {},
        Act::Command, "sim_new_fatigue");
    add(GStruct, "study_contact", "Contact", "Nonlinear contact check (CalculiX).",
        webNoPy || ccxMissing, webNoPy ? webHint : ccxHint, Act::Command, "sim_new_contact");
    add(GStruct, "study_vibration", "Vibration", "Response to harmonic base shaking.",
        webNoPy || vibMissing, webNoPy ? webHint : vibHint, Act::Command, "sim_new_freqresp");
    add(GThermal, "study_thermal_fea", "Thermal FEA", "Conduction, steady or transient.", false,
        {}, Act::Command, "sim_new_thermal_fea");
    add(GThermal, "study_thermal_cfd", "Thermal CFD", "Convection and conjugate heat (CFD).",
        webNoPy || openfoamMissing,
        webNoPy ? webHint : std::string("Needs OpenFOAM; use Thermal FEA."), Act::Command,
        "sim_new_thermal");
    add(GFluids, "study_flow", "Flow", "External flow: drag and lift.", webNoPy, webHint,
        Act::Command, "sim_new_flow");
    add(GFluids, "fluid", "Internal flow", "Pressure drop through a pipe run.", false, {},
        Act::Command, "sim_piping_flow");
  }
  if (hasAsm)
    add(GMotion, "rotate_component", "Motion", "Drive mates, check interference.", false, {},
        Act::Command, "sim_new_motion");
  if (mechDoc)
    add(GEm, "study_em", "Electromagnetic", "RF modes, S-parameters, time domain.",
        emStatus_ < 0, "EM solver not installed.", Act::Command, "sim_em_eigen");
  if (hasSch || hasPcb)
    add(GElec, "sch_simulate", "Circuit (SPICE)", "DC, AC and transient analysis.", !hasSch,
        "Open the schematic tab.", Act::Electronics, nullptr);
  const bool fwProjects = fwsim_.projectsLoaded && !fwsim_.projectIds.empty();
  if (hasSch || hasPcb || fwProjects)
    add(GElec, "cat_electronics", "Firmware emulation", "Run firmware in an emulator.",
        fwStatus_ == 1 && !fwCanEmulate_,
        fwHint_.empty() ? std::string("Install Renode or QEMU.") : fwHint_, Act::Firmware,
        nullptr);
  if (mechDoc) {
    const bool topoAsm = activeDocIsAssembly(st);
    const bool topoUnavailable = topoCaps_.state == topology::Capabilities::State::Unavailable;
    add(GOpt, "study_topology", "Topology", "Optimize this part's material layout.",
        topoAsm || topoUnavailable,
        topoAsm ? std::string("Open a part, not an assembly.") : topoCaps_.error, Act::Command,
        "sim_new_topology");
    add(GIndustrial, "study_piping", "Pipe network", "Route, solve and code-check pipes.", false,
        {}, Act::Command, "sim_piping_new");
    add(GIndustrial, "study_hydraulics", "Hydraulic circuit", "Size and simulate fluid power.",
        false, {}, Act::Command, "sim_hyd_new");
    add(GIndustrial, "vessel", "Pressure vessel", "Shell and head thickness to code.", false, {},
        Act::Command, "sim_piping_vessel");
    add(GIndustrial, "duct", "Duct system", "Duct sizing and pressure loss.", false, {},
        Act::Command, "sim_piping_duct");
  }

  // Group order is fixed; entries keep their insertion order within a group.
  std::vector<kit::PickerItem> items;
  std::vector<const Entry *> flat;
  std::vector<kit::PickerGroup> groups;
  items.reserve(entries.size());
  for (int g = 0; g < GCount; ++g) {
    const size_t first = items.size();
    for (const auto &e : entries) {
      if (e.group != g)
        continue;
      kit::PickerItem it;
      it.icon = e.icon;
      it.title = e.title;
      it.desc = e.desc;
      it.enabled = e.enabled;
      it.reason = e.reason.empty() ? nullptr : e.reason.c_str();
      items.push_back(it);
      flat.push_back(&e);
    }
    if (items.size() > first)
      groups.push_back({kGroupTitles[g], nullptr, (int)(items.size() - first)});
  }
  // Items are final now: point each group at its slice.
  {
    size_t off = 0;
    for (auto &g : groups) {
      g.items = items.data() + off;
      off += (size_t)g.count;
    }
  }

  pushCardText(); // the popup sits on the theme glass, not the pane backdrop
  ImGui::PushFont(st.fonts.medium);
  ImGui::TextUnformatted("New study");
  ImGui::PopFont();
  int picked = -1;
  if (groups.empty())
    kit::emptyState("study_static", "Nothing to simulate yet",
                    "Open a part, assembly, schematic, PCB or firmware project to see the study "
                    "kinds it unlocks.");
  else
    picked = kit::groupedPicker("new_study", groups.data(), (int)groups.size(),
                                ui::metrics::px(420.0f), pickerMaxH);
  popCardText();
  ImGui::EndPopup();
  ImGui::PopStyleVar(2);
  ui::popGlassWindowStyle();

  // Act after the popup closed so routed commands (which swap the pane) never run mid-popup.
  if (picked < 0 || picked >= (int)flat.size())
    return;
  const Entry &e = *flat[picked];
  if (e.act == Act::Electronics)
    openElectronicsPanel(st);
  else if (e.act == Act::Firmware)
    openEmulationPanel(st);
  else
    handleCommand(st, e.cmd);
}

// ---- panel header ----------------------------------------------------------------------------------
bool SimManager::drawPanelHeader(AppState &st, Study &s) {
  const SimRunUi live = runUi();
  const StudyStatusView sv = studyStatusView(s, live.running && live.studyId == s.id, {});
  // Units popover: a text chip naming the active system ("SI" / "US"), accent when this session
  // overrides the document's units; the tooltip spells it out.
  const int unitsMode = (st.simUnits < 0 || st.simUnits > 2) ? 0 : st.simUnits;
  const bool unitsUs = unitsMode == 2 || (unitsMode == 0 && st.ips());
  const char *unitsTip = unitsUs ? "Display units: US customary (psi \xc2\xb7 lbf \xc2\xb7 in). "
                                   "Click to change."
                                 : "Display units: SI (MPa \xc2\xb7 N \xc2\xb7 mm). Click to change.";
  const kit::HeaderAction acts[] = {{"units", nullptr, unitsTip, true,
                                     unitsMode == 0 ? kit::Tone::Neutral : kit::Tone::Accent,
                                     unitsUs ? "US" : "SI"}};
  int hit = -1;
  const bool done = simPanelHeader(st, studyTypeIcon(s.type), s.name.c_str(),
                                   studyTypeLabel(s.type), sv.chip, sv.tone, nullptr, acts, 1,
                                   &hit, sv.tip.empty() ? nullptr : sv.tip.c_str());
  if (hit == 0)
    ImGui::OpenPopup("##sim_units");

  // Display/input units (the solver always computes in SI). "Follow document" tracks the part's
  // MMGS/IPS setting.
  ui::pushGlassWindowStyle();
  ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, kit::rowRound());
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(kit::padX(), kit::padY()));
  if (ImGui::BeginPopup("##sim_units")) {
    pushCardText();
    struct Opt {
      const char *title, *detail;
    };
    const bool docUs = st.ips();
    const Opt opts[3] = {
        {"Follow document", docUs ? "This part is IPS: psi \xc2\xb7 lbf \xc2\xb7 in"
                                  : "This part is MMGS: MPa \xc2\xb7 N \xc2\xb7 mm"},
        {"SI", "MPa \xc2\xb7 N \xc2\xb7 mm"},
        {"US customary", "psi \xc2\xb7 lbf \xc2\xb7 in"}};
    // Pin the popup width to its widest line so the full-width rows do not collapse it.
    float w = 0.0f;
    for (const auto &o : opts)
      w = std::max({w, ImGui::CalcTextSize(o.title).x, ImGui::CalcTextSize(o.detail).x});
    ImGui::Dummy(ImVec2(w + kit::padX() * 2.0f, 0.0f));
    const int cur = (st.simUnits < 0 || st.simUnits > 2) ? 0 : st.simUnits;
    int chosen = -1;
    for (int i = 0; i < 3; ++i) {
      ImGui::PushID(i);
      kit::RowStyle rs;
      rs.secondary = opts[i].detail;
      rs.selected = cur == i;
      if (kit::row("unit", opts[i].title, rs))
        chosen = i;
      ImGui::PopID();
    }
    popCardText();
    if (chosen >= 0) {
      ImGui::CloseCurrentPopup();
      if (chosen != cur) {
        st.simUnits = chosen;
        // Keep the cached legend labels current even while the colormap is hidden.
        if (s.type == StudyType::Topology) {
          // Re-derive the topology legend (its fields carry their own SI units).
          if (!topoResultsView_.selectedRunId.empty() && !s.overlayPath.empty())
            topoActions_.push_back(
                {s.id, {topology::ResultsAction::Kind::ShowRun, topoResultsView_.selectedRunId,
                        ""}});
        } else if (s.hasResult && st.renderer) {
          std::string label, unit;
          double fromSI = 1.0;
          fieldMeta(st, st.activeScalarField, label, unit, fromSI);
          st.legendTitle = label;
          st.legendUnits = unit;
          float vmin = 0, vmax = 1;
          if (st.renderer->scalarRange(vmin, vmax)) {
            st.legendMin = (float)(vmin * fromSI);
            st.legendMax = (float)(vmax * fromSI);
          }
        }
      }
    }
    ImGui::EndPopup();
  }
  ImGui::PopStyleVar(2);
  ui::popGlassWindowStyle();
  return !done;
}

// ---- study rows -------------------------------------------------------------------------------------
void SimManager::drawStudyRow(AppState &st, int i) {
  Study &s = studies_[i];
  // Stable scope: the engine-assigned study id, never the (renamable) name or a count.
  if (s.id.empty())
    ImGui::PushID(i);
  else
    ImGui::PushID(s.id.c_str());
  const bool selStudy = sel_ == i;
  const SimRunUi live = runUi();
  const bool running = live.running && live.studyId == s.id;

  // Setup readiness only (busy/other-document reasons belong to the Run button, not the row).
  std::string setupIssue;
  if (studyReady(s, setupIssue)) {
    if (s.type == StudyType::Topology)
      topologyReady(st, s, setupIssue);
    else if (s.type == StudyType::Motion)
      motionJointsReady(s, st.docMirror.tree(), setupIssue);
  }
  const StudyStatusView sv = studyStatusView(s, running, setupIssue);

  // ---- the study row: icon, elided name, dim type, status mark; Run / Delete on hover ----
  const float lineH = ImGui::GetTextLineHeight();
  const float rowH = lineH + kit::padY() * 2.0f;
  const float rowW = ImGui::GetContentRegionAvail().x;
  const ImVec2 p0 = ImGui::GetCursorScreenPos();
  const ImVec2 p1(p0.x + rowW, p0.y + rowH);
  const float btn = std::min(ImGui::GetFrameHeight(), rowH);
  const float gap = ui::metrics::U(0.5f);
  const bool confirming = pendingDeleteStudyId_ == s.id;
  const bool hoverRow = ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
                        ImGui::IsMouseHoveringRect(p0, p1);
  const bool showActions = (hoverRow || confirming) && !running;
  const float markW = lineH;
  kit::RowStyle rs;
  rs.icon = studyTypeIcon(s.type);
  // Default names are "<Type> N"; the dim type only adds information on a renamed study.
  const char *typeLabel = studyTypeLabel(s.type);
  rs.meta = s.name.rfind(typeLabel, 0) == 0 ? nullptr : typeLabel;
  rs.selected = selStudy && node_ == SimNode::Study;
  rs.actionsW = markW + (showActions ? btn * 2.0f + gap * 2.0f : 0.0f);
  ImGui::SetNextItemAllowOverlap(); // the action lane is submitted on top of the row's hit box
  const bool clicked = kit::row("study", s.name.c_str(), rs);
  const bool dbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
  bool wantDelete = false, wantRun = false, wantOpen = false;
  ui::pushContextMenuStyle();
  if (ImGui::BeginPopupContextItem("##study_ctx")) {
    if (ImGui::MenuItem("Open setup"))
      wantOpen = true;
    const std::string why = runBlockerFor(st, s);
    if (ImGui::MenuItem("Run", nullptr, false, why.empty()))
      wantRun = true;
    if (!why.empty())
      kit::itemTooltip(why.c_str());
    if (ImGui::MenuItem("Delete\xe2\x80\xa6", nullptr, false, !running))
      pendingDeleteStudyId_ = s.id;
    ImGui::EndPopup();
  }
  ui::popContextMenuStyle();
  const ImVec2 after = ImGui::GetCursorScreenPos();

  const float laneX0 = p1.x - kit::padX() - rs.actionsW;
  const bool overLane = ImGui::IsMouseHoveringRect(ImVec2(laneX0, p0.y), p1, false);
  statusMark(ImVec2(p1.x - kit::padX() - markW * 0.5f, p0.y + rowH * 0.5f), markW, sv.status,
             sv.tip);
  if (showActions) {
    const float by = p0.y + (rowH - btn) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(laneX0, by));
    const std::string why = runBlockerFor(st, s);
    if (kit::iconButton("run", "run_study", why.empty() ? "Run this study" : why.c_str(),
                        why.empty(), kit::Tone::Neutral, btn))
      wantRun = true;
    ImGui::SetCursorScreenPos(ImVec2(laneX0 + btn + gap, by));
    if (kit::iconButton("delete", "delete", "Delete this study", true, kit::Tone::Neutral, btn))
      pendingDeleteStudyId_ = s.id;
    ImGui::SetCursorScreenPos(after);
  }
  if ((clicked || dbl) && !overLane) {
    sel_ = i;
    node_ = SimNode::Study;
    armedWell_ = -1;
    if (dbl)
      wantOpen = true;
  }

  // Inline delete confirmation (no modal): the study and its results leave the document.
  if (confirming) {
    ImGui::Indent(kit::padX());
    const std::string q = "Delete \"" + s.name + "\"? Its setup and results are removed from "
                          "this document.";
    kit::inlineStatus(q.c_str(), kit::Tone::Warn);
    if (kit::dangerButton("Delete###del_yes", !running))
      wantDelete = true;
    ImGui::SameLine();
    if (kit::secondaryButton("Cancel###del_no"))
      pendingDeleteStudyId_.clear();
    ImGui::Unindent(kit::padX());
  }

  // ---- setup nodes of the selected study (single-click opens/focuses the existing editor;
  // double-click also arms Fixtures / Loads). Rows reuse the stepper's readiness model; a step
  // needing attention shows its reason on a dim second line. IDs are fixed keys, never labels.
  if (selStudy && !confirming) {
    ImGui::Indent(ui::metrics::U(3.0f));
    auto child = [&](const char *key, int idx, const char *icon, const std::string &label,
                     SimNode n, kit::Status cs, const std::string &tip, bool showReason) {
      ImGui::PushID(key);
      if (idx >= 0)
        ImGui::PushID(idx);
      kit::RowStyle cr;
      cr.icon = icon;
      cr.selected = node_ == n && (n != SimNode::Load || loadIdx_ == idx);
      cr.secondary = showReason && !tip.empty() ? tip.c_str() : nullptr;
      cr.tooltip = tip.empty() ? nullptr : tip.c_str();
      cr.actionsW = markW;
      const ImVec2 c0 = ImGui::GetCursorScreenPos();
      const float cw = ImGui::GetContentRegionAvail().x;
      const bool hit = kit::row("node", label.c_str(), cr);
      const bool cdbl = ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
      // The mark sits on the title line (rows with a reason line are taller).
      statusMark(ImVec2(c0.x + cw - kit::padX() - markW * 0.5f, c0.y + kit::padY() + lineH * 0.5f),
                 markW, cs, tip);
      if (hit || cdbl) {
        node_ = n;
        loadIdx_ = n == SimNode::Load
                       ? (idx >= 0 ? idx : (s.loads.empty() ? -1 : 0))
                       : idx;
        runPage_ = RunPageView{};
        panelOpen_ = true;
        // Clicking a checklist row opens its editor; double-click additionally arms a genuine
        // face well. During a run the setup stays inspectable but never captures new picks.
        const int newArm = !running && cdbl
                               ? (n == SimNode::Fixtures ? 0
                                  : n == SimNode::Load && loadIdx_ >= 0 ? loadIdx_ + 1 : -1)
                               : -1;
        if (newArm >= 0 && newArm != armedWell_)
          st.selection.clear();
        armedWell_ = newArm;
        const char *focus = nullptr;
        switch (n) {
        case SimNode::Material:
          focus = isFlowFamily(s.type) ? "fluid" : "material";
          break;
        case SimNode::Fixtures:
          focus = "fixtures";
          break;
        case SimNode::Load:
          focus = "load";
          break;
        case SimNode::Mesh:
          focus = isFlowFamily(s.type) ? "resolution" : "mesh";
          break;
        case SimNode::Boundaries:
          focus = isFlowFamily(s.type) ? "flow" : nullptr;
          break;
        case SimNode::Study:
          focus = s.type == StudyType::Fatigue ? "cyclic"
                  : s.type == StudyType::Modal || s.type == StudyType::Buckling ? "modes"
                  : s.type == StudyType::ThermalFea ? "conduction"
                  : s.type == StudyType::Contact ? "contact"
                  : s.type == StudyType::FreqResponse ? "excitation"
                  : s.type == StudyType::Motion ? "drivers" : nullptr;
          break;
        default:
          break;
        }
        requestFocus(focus);
      }
      if (idx >= 0)
        ImGui::PopID();
      ImGui::PopID();
    };
    // SetupStep::status: 0 ready, 1 optional / not set, 2 needs attention.
    auto stepMark = [](int status) {
      return status == 0 ? kit::Status::Done
                         : (status == 1 ? kit::Status::Pending : kit::Status::Retry);
    };
    const bool topo = s.type == StudyType::Topology;
    const bool cfd = isFlowFamily(s.type);
    std::string label;
    for (const SetupStep &step : setupSteps(st, s)) {
      const char *key = "study";
      const char *icon = studyTypeIcon(s.type);
      label = step.title ? step.title : step.label ? step.label : "";
      switch (step.node) {
      case SimNode::Material:
        key = "material";
        icon = cfd ? "fluid" : "material";
        if (topo) {
          nlohmann::json mat = nlohmann::json::object();
          std::string matErr;
          label = "Material: " +
                  (topology::runMaterial(s.topo, topologyMaterialLookup(), mat, matErr)
                       ? mat.value("name", std::string("custom"))
                       : std::string("not set"));
        } else if (!cfd) {
          const int mi = materialIndexByKey(s.materialKey);
          label = "Material: " +
                  (s.definition.contains("material") && s.definition["material"].is_object()
                       ? std::string("native properties")
                       : mi >= 0 ? materials()[mi].name
                       : !s.materialKey.empty() ? materialDisplayName(s.materialKey)
                                                : std::string("not set"));
        }
        break;
      case SimNode::Fixtures:
        key = "fixtures";
        icon = "fixture";
        label += " (" + s.fixtures.label() + ")";
        break;
      case SimNode::Load:
        key = "loads";
        icon = s.type == StudyType::ThermalFea ? "load_heat" : "load_force";
        label += " (" + std::to_string(s.loads.size()) + ")";
        break;
      case SimNode::Mesh:
        key = "mesh";
        icon = "mesh";
        if (topo)
          label = "Mesh (linear tetrahedra)";
        break;
      case SimNode::Boundaries:
        key = "boundaries";
        icon = cfd ? "flow_bc" : "fixture";
        break;
      case SimNode::Regions:
        key = "regions";
        icon = "fixture";
        break;
      default:
        if (topo) {
          label = step.label ? step.label : "Study";
          const nlohmann::json settings = s.topo.value("topology", nlohmann::json());
          if (settings.is_object()) {
            const std::string m = settings.value("method", std::string());
            const std::string p = settings.value("physics", std::string());
            if (!p.empty())
              label += ": " + topology::physicsLabel(p, &topoCaps_);
            if (!m.empty())
              label += ", " + topology::methodLabel(m, &topoCaps_);
          }
        }
        break;
      }
      child(key, -1, icon, label, step.node, stepMark(step.status), step.issue,
            step.status == 2);
      // Each load stays addressable (click opens its editor, double-click arms its face well).
      if (step.node == SimNode::Load && !s.loads.empty()) {
        ImGui::Indent(ui::metrics::U(2.0f));
        for (int li = 0; li < (int)s.loads.size(); ++li) {
          const SimLoad &ld = s.loads[li];
          const char *kind = s.type == StudyType::ThermalFea
                                 ? (ld.thermalKind == 1 ? "Convection " : "Heat flux ")
                                 : (ld.pressure ? "Pressure " : "Force ");
          const bool noFaces = !nativeLoadHasFaces(ld);
          child("load", li, ld.pressure ? "load_pressure" : icon,
                kind + std::to_string(li + 1) + " (" + ld.faces.label() + ")", SimNode::Load,
                noFaces ? kit::Status::Retry : kit::Status::Pending,
                noFaces ? std::string("No faces picked: open the editor to pick or drop them.")
                        : std::string(),
                noFaces);
        }
        ImGui::Unindent(ui::metrics::U(2.0f));
      }
    }
    const int nRuns = topo && s.topo.contains("runs") && s.topo["runs"].is_array()
                          ? (int)s.topo["runs"].size()
                          : 0;
    const bool solved = topo ? nRuns > 0 : (s.hasResult || s.motionHasResult);
    child("results", -1, "results",
          nRuns > 0 ? "Results (" + std::to_string(nRuns) + (nRuns == 1 ? " run)" : " runs)")
                    : std::string("Results"),
          SimNode::Results, solved ? kit::Status::Done : kit::Status::Pending,
          solved ? std::string() : std::string("Run the study to generate results."), false);
    ImGui::Unindent(ui::metrics::U(3.0f));
  }
  ImGui::PopID();

  if (wantOpen) {
    sel_ = i;
    node_ = SimNode::Study;
    loadIdx_ = -1;
    armedWell_ = -1;
    panelOpen_ = true;
  }
  if (wantRun) {
    sel_ = i; // the run pipeline keys its in-flight study off the selection
    run(st, studies_[i]);
  }
  if (wantDelete) {
    pendingDeleteStudyId_.clear();
    deleteStudy(st, i);
  }
}

} // namespace ui
