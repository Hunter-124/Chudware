#pragma once
// Framework-agnostic view-model for the T-R5 in-editor PLM surface (history
// timeline panel, doc-tab revision badges, checkpoint/release toolbar, compare
// view). Mirrors ui/abstract/bom_view.h / parts_view.h: pure structs + inline
// helpers with no ImGui/GL/JSON includes — ui/imgui/src/ui/plm_panel.cpp parses
// the plm_* worker envelopes (contract aicad.plm/1 §7) into these and renders
// from them; a future backend can reuse them unchanged.
//
// Ease-of-use law (contract §8.1, normative): nothing in this header may
// surface git vocabulary. Entry ids are machine handles (the contract's
// internal `commit` field) — they are NEVER rendered to the user.

#include <cstddef>
#include <string>
#include <vector>

namespace aicad::ui {

// One history entry (contract §7 plm_history entry, GUI view).
struct PlmEntryView {
    std::string id;         // machine handle for restore/compare calls — never shown
    std::string kind;       // "auto" | "checkpoint" | "release" | "restore"
    std::string whenIso;    // authored_at (ISO 8601, UTC)
    std::string author;     // display name ("User", "Copilot", ...)
    bool        aiAuthored = false;  // §8.4: AI entries are visibly attributed
    std::string message;    // checkpoint/release note ("" for auto saves)
    std::string summary;    // T-R3 plain-language change summary
    std::string thumbFile;  // absolute path of the exported thumbnail ("" = none)
};

// One project item (contract §4 registry view) for badges + release flow.
struct PlmItemView {
    std::string itemId;
    std::string path;        // container file name within the project folder
    std::string docKind;     // part | assembly | drawing | ecad | cam | ...
    std::string partNumber;
    std::string rev;         // last released letter ("" = never released)
    std::string lifecycle;   // "In work" | "Released" | "Superseded"
    std::string workingRev;  // the letter the NEXT release will stamp ("" if Released)
    bool        dirty = false;  // unsaved changes vs the latest history entry
};

// A rendered timeline row: either one story entry, or a collapsed group of
// consecutive auto-saves between story rows (T-R5 locked decision: checkpoints
// + releases are the story; auto-saves collapse into an expandable "N saves").
struct PlmTimelineRow {
    bool isGroup = false;
    int  first = 0;   // index into the entries vector (newest-first order)
    int  count = 1;   // group size (1 for story rows)
};

// Collapse consecutive `auto` runs of >= 2 into group rows. Entries are
// newest-first (the plm_history order); single autos stay ordinary rows.
inline std::vector<PlmTimelineRow>
buildTimelineRows(const std::vector<PlmEntryView>& entries) {
    std::vector<PlmTimelineRow> rows;
    const int n = (int)entries.size();
    for (int i = 0; i < n;) {
        if (entries[(size_t)i].kind == "auto") {
            int j = i;
            while (j < n && entries[(size_t)j].kind == "auto") ++j;
            PlmTimelineRow r;
            r.isGroup = (j - i) >= 2;
            r.first = i;
            r.count = j - i;
            rows.push_back(r);
            i = j;
        } else {
            rows.push_back(PlmTimelineRow{false, i, 1});
            ++i;
        }
    }
    return rows;
}

// Short badge text for a doc tab: the released letter, or the draft letter.
// "" = no badge (item unknown / never saved into a project).
inline std::string plmBadgeText(const PlmItemView& it) {
    if (it.lifecycle == "Released" && !it.rev.empty()) return it.rev;
    if (!it.workingRev.empty()) return it.workingRev;
    return "";
}

// Plain-language lifecycle line for tooltips / the FM strip.
inline std::string plmLifecycleLabel(const PlmItemView& it) {
    if (it.lifecycle == "Released" && !it.rev.empty())
        return "Rev " + it.rev + " \xc2\xb7 Released";
    if (!it.workingRev.empty()) {
        if (!it.rev.empty())
            return "Rev " + it.workingRev + " draft \xc2\xb7 In work (rev " +
                   it.rev + " is released)";
        return "Rev " + it.workingRev + " draft \xc2\xb7 In work";
    }
    return it.lifecycle;
}

// The one-line story label for an entry row (message wins; else summary;
// else a kind-appropriate placeholder — never blank, never jargon).
inline std::string plmEntryTitle(const PlmEntryView& e) {
    if (!e.message.empty()) return e.message;
    if (!e.summary.empty()) return e.summary;
    if (e.kind == "auto") return "Saved";
    if (e.kind == "restore") return "Returned to an earlier version";
    if (e.kind == "release") return "Released";
    return "Checkpoint";
}

// "12 saves" group label.
inline std::string plmGroupLabel(int count) {
    return std::to_string(count) + " saves";
}

}  // namespace aicad::ui
