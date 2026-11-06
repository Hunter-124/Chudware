// Exercise the real PM field/patch code without starting the GUI or geometry worker.
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <cassert>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>

// Use the production field implementation without linking desktop lifecycle/rendering code.
#include "../src/ui/property_manager_fields.inl"

namespace ImGui {
// flushDimText only needs a monotonic editing timestamp; no ImGui context is created.
double GetTime() { return 1.0; }
}

namespace ui {
struct PropertyManagerTestAccess {
    static void open(PropertyManager& pm, const char* type, bool edit = false, bool ips = false) {
        pm.form_ = pmFindForm(type);
        assert(pm.form_);
        pm.active_ = true;
        pm.editMode_ = edit;
        pm.targetId_ = edit ? "f1" : "";
        pm.title_ = pm.form_->title;
        pm.ips_ = ips;
        pm.fields_.resize(pm.form_->fields.size());
        for (size_t i = 0; i < pm.fields_.size(); ++i)
            initField(pm.fields_[i], pm.form_->fields[i], ips);
        if (ips && !edit) applyIpsNativeDefaults(pm.form_, pm.fields_);
    }
    static PMField& field(PropertyManager& pm, const char* key,
                          PMFieldKind kind, int occurrence = 0) {
        for (auto& f : pm.fields_) {
            if (f.def->kind == kind && std::strcmp(f.def->key, key) == 0 && occurrence-- == 0)
                return f;
        }
        assert(false && "field not found");
        std::abort();
    }
    static bool ready(PropertyManager& pm) { return readyForPreview(pm.form_, pm.fields_); }
    static bool flush(PropertyManager& pm) { return pm.flushDimText(); }
    static bool visible(PropertyManager& pm, PMField& field) {
        return fieldVisible(*field.def, pm.fields_);
    }
    static void load(PropertyManager& pm, const nlohmann::json& feature) { pm.loadFromTree(feature); }
    static void hydrateFillet(PropertyManager& pm, const nlohmann::json& evidence) {
        pm.hydrateFilletEvidence(evidence);
    }
    static void units(PropertyManager& pm, bool ips) { pm.ips_ = ips; pm.reformatDimDisplays(); }
    static nlohmann::json feature(PropertyManager& pm, bool final = false) {
        pm.committing_ = final;
        nlohmann::json result;
        pm.buildOpFeature(result);
        return result;
    }
};
}

namespace {
using namespace ui;
using Access = PropertyManagerTestAccess;
using json = nlohmann::json;

void text(PMField& f, const char* value, bool ips) {
    std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", value);
    f.dimInputIps = ips;
    f.dimTextDirty = true;
}
void choose(PMField& f, const char* option) {
    f.comboIndex = pmComboIndexOf(*f.def, option);
    assert(f.comboIndex >= 0);
    f.comboUnsupported.clear();
}
void pick(PMField& f, const json& selector = {{"kind", "face"}}) {
    PMSelEntry entry; entry.selector = selector;
    f.picks.push_back(std::move(entry));
}

void parse_units_and_boundaries() {
    double v = -9.0; std::string error;
    assert(pmParseDim("1mm", v, error, false, 25.4) && v == 1.0);
    assert(pmParseDim("(1+1)mm", v, error, false, 25.4) && v == 2.0);
    assert(pmParseDim("1/2in", v, error, false, 25.4) && std::abs(v - 12.7) < 1e-12);
    assert(pmParseDim(".75/2", v, error, false, 25.4) && std::abs(v - 9.525) < 1e-12);
    assert(pmParseDim("1mm+2in", v, error, false, 25.4) && std::abs(v - 51.8) < 1e-12);
    assert(pmParseDim("90deg", v, error, true, 25.4) && v == 90.0);
    v = 42.0;
    assert(!pmParseDim("1e308in", v, error, false, 25.4) && v == 42.0);
    assert(!pmParseDim("1/0", v, error, false, 1.0) && v == 42.0);
    assert(!pmParseDim("1deg", v, error, false, 1.0) && v == 42.0);

    PropertyManager sm; Access::open(sm, "sm_base_flange", false, true);
    auto& k = Access::field(sm, "k_factor", PMFieldKind::Dim);
    text(k, "0", true); assert(parseFieldText(k, true) && k.dimValue == 0.0);
    text(k, "1", true); assert(parseFieldText(k, true) && k.dimValue == 1.0);
    text(k, "0.5", true); assert(parseFieldText(k, true) && k.dimValue == 0.5);
    text(k, "-0.1", true); assert(!parseFieldText(k, true) && k.dimValue == 0.5);
    text(k, "2", true); assert(!parseFieldText(k, true) && k.dimValue == 0.5);
    text(k, "0.5mm", true); assert(!parseFieldText(k, true) && k.dimValue == 0.5);
    text(k, "=  ", true); assert(!parseFieldText(k, true) && k.dimValue == 0.5);
    text(k, "=thickness/2", true); assert(parseFieldText(k, true));
    assert(k.dimExpr == "thickness/2");
    for (bool ips : {false, true}) {
        PropertyManager box; Access::open(box, "box", false, ips);
        auto& length = Access::field(box, "length", PMFieldKind::Dim);
        assert(applyAiDimPatch(length, 1.0, ips));
        const double aiLength = length.dimValue;
        const json aiNumber = Access::feature(box)["params"]["length"];
        text(length, "1", ips); assert(parseFieldText(length, ips));
        assert(length.dimValue == aiLength && aiLength == (ips ? 25.4 : 1.0));
        assert(Access::feature(box)["params"]["length"] == aiNumber);
        assert(Access::feature(box)["params"]["length"] ==
               json({{"value", ips ? 25.4 : 1.0}, {"expr", nullptr}}));
        assert(applyAiDimPatch(length, json{{"expr", "width/2"}}, ips));
        const json aiExpression = Access::feature(box)["params"]["length"];
        assert(aiExpression["expr"] == "width/2");
        text(length, "=width/2", ips); assert(parseFieldText(length, ips));
        assert(Access::feature(box)["params"]["length"] == aiExpression);

        PropertyManager rev; Access::open(rev, "revolve", false, ips);
        auto& angle = Access::field(rev, "angle", PMFieldKind::Dim);
        assert(applyAiDimPatch(angle, 45.0, ips) && angle.dimValue == 45.0);
        text(angle, "45", ips); assert(parseFieldText(angle, ips) && angle.dimValue == 45.0);

        PropertyManager sheet; Access::open(sheet, "sm_base_flange", false, ips);
        auto& ratio = Access::field(sheet, "k_factor", PMFieldKind::Dim);
        assert(applyAiDimPatch(ratio, 0.5, ips) && ratio.dimValue == 0.5);
        text(ratio, "0.5", ips); assert(parseFieldText(ratio, ips) && ratio.dimValue == 0.5);
        pick(Access::field(sheet, "profile", PMFieldKind::SelectorWell));
        assert(Access::ready(sheet));
        assert(!applyAiDimPatch(ratio, "2", ips) && ratio.dimValue == 0.5);
        assert(!Access::ready(sheet));
        text(ratio, "0.5", ips);
        assert(parseFieldText(ratio, ips) && Access::ready(sheet));
    }
}

// OK flushes the current editor before readiness/serialization; it must use the
// units in which a pending edit began, not a subsequently selected display unit.
void pending_text_flushes_into_the_feature() {
    PropertyManager box; Access::open(box, "box", false, false);
    auto& length = Access::field(box, "length", PMFieldKind::Dim);
    text(length, "2", false);
    assert(!Access::ready(box));
    Access::units(box, true);
    assert(Access::flush(box));
    assert(Access::ready(box) && box.previewPending());
    assert(Access::feature(box)["params"]["length"] ==
           json({{"value", 2.0}, {"expr", nullptr}}));
    double displayed = 0.0; std::string error;
    assert(length.dimInputIps);
    assert(pmParseDim(length.dimBuf.data(), displayed, error, false, 25.4));
    assert(std::abs(displayed - 2.0) < 1e-4); // accepted old-unit text was reformatted on blur

    text(length, "bad", true);
    assert(!Access::flush(box));
    assert(!Access::ready(box) && length.dimValue == 2.0);
    text(length, "1/2", true);
    assert(Access::flush(box) && Access::ready(box));
    assert(std::abs(length.dimValue - 12.7) < 1e-12);
    assert(std::abs(Access::feature(box)["params"]["length"]["value"].get<double>() - 12.7) < 1e-12);
}

void readiness_tracks_all_visible_fields_and_references() {
    PropertyManager box; Access::open(box, "box");
    assert(Access::ready(box));
    auto& width = Access::field(box, "width", PMFieldKind::Dim);
    text(width, "bad", false); assert(!parseFieldText(width, false));
    assert(!Access::ready(box));
    text(width, "5mm", false); assert(parseFieldText(width, false));
    assert(Access::ready(box));

    PropertyManager extrude; Access::open(extrude, "extrude");
    pick(Access::field(extrude, "profile", PMFieldKind::SelectorWell));
    auto& depth = Access::field(extrude, "depth", PMFieldKind::Dim);
    text(depth, "=", false); assert(!parseFieldText(depth, false));
    assert(!Access::ready(extrude));
    choose(Access::field(extrude, "end", PMFieldKind::Combo), "through_all");
    assert(Access::ready(extrude)); // hidden invalid depth must not block through-all
    assert(Access::flush(extrude)); // a hidden invalid editor is not flushed

    PropertyManager legacy; Access::open(legacy, "extrude", true);
    Access::load(legacy, {{"params", {{"end", "up_to_next"}}},
                          {"refs", {{"profile", json::array({{{"kind", "face"}}})}}}});
    assert(!Access::ready(legacy)); // unknown existing choice is never silently changed to blind
    auto& legacyEnd = Access::field(legacy, "end", PMFieldKind::Combo);
    assert(legacyEnd.comboUnsupported == "up_to_next");
    choose(legacyEnd, "blind"); assert(Access::ready(legacy));

    PropertyManager plane; Access::open(plane, "plane");
    assert(!Access::ready(plane));
    pick(Access::field(plane, "base", PMFieldKind::SelectorWell), "plane.top");
    auto& offset = Access::field(plane, "offset", PMFieldKind::Dim);
    text(offset, "0", false); assert(parseFieldText(offset, false) && Access::ready(plane));
    text(offset, "-3", false); assert(parseFieldText(offset, false) && Access::ready(plane));
    choose(Access::field(plane, "method", PMFieldKind::Combo), "three_point");
    auto& points = Access::field(plane, "points", PMFieldKind::SelectorWell);
    pick(points); pick(points); assert(!Access::ready(plane));
    pick(points); assert(Access::ready(plane));
    choose(Access::field(plane, "method", PMFieldKind::Combo), "midplane");
    assert(!Access::ready(plane));
    pick(Access::field(plane, "second", PMFieldKind::SelectorWell, 1), "plane.front");
    assert(Access::ready(plane));

    PropertyManager csys; Access::open(csys, "csys"); assert(Access::ready(csys));
    PropertyManager thread; Access::open(thread, "thread"); assert(!Access::ready(thread));
    pick(Access::field(thread, "target_face", PMFieldKind::SelectorWell));
    assert(Access::ready(thread));
}

void visibility_is_transitive_and_cycle_safe() {
    PropertyManager ext; Access::open(ext, "extrude", true);
    Access::load(ext, {{"params", {{"end", "blind"}, {"draft_outward", false},
                                   {"draft", {{"value", 3.0}, {"expr", nullptr}}}}},
                       {"refs", {{"profile", json::array({{{"kind", "face"}}})}}}});
    auto& end = Access::field(ext, "end", PMFieldKind::Combo);
    auto& draftOn = Access::field(ext, "draft_outward", PMFieldKind::Check);
    auto& draft = Access::field(ext, "draft", PMFieldKind::Dim);
    assert(!draftOn.checkValue && Access::visible(ext, draft) && Access::ready(ext));
    auto inward = Access::feature(ext);
    assert(inward.at("params").at("draft_outward") == false);
    assert(inward.at("params").at("draft").at("value") == 3.0);
    choose(end, "through_all");
    assert(!Access::visible(ext, draftOn) && !Access::visible(ext, draft));
    assert(Access::feature(ext).at("params").at("draft").is_null());
    choose(end, "blind");
    assert(Access::visible(ext, draft) && Access::feature(ext).at("params").at("draft").at("value") == 3.0);

    PropertyManager chamfer; Access::open(chamfer, "chamfer");
    pick(Access::field(chamfer, "edges", PMFieldKind::SelectorWell));
    assert(Access::ready(chamfer));
    choose(Access::field(chamfer, "type", PMFieldKind::Combo), "vertex");
    assert(!Access::ready(chamfer));
    pick(Access::field(chamfer, "vertices", PMFieldKind::SelectorWell));
    assert(Access::ready(chamfer));

    PropertyManager reopened; Access::open(reopened, "chamfer", true);
    Access::load(reopened, {{"params", {{"type", "vertex"}}},
                            {"refs", {{"vertices", json::array({{{"kind", "vertex"}}})}}}});
    assert(Access::ready(reopened));
    assert(Access::feature(reopened)["refs"]["vertices"].size() == 1);
    PMFieldDef top, middle, bottom;
    top.kind = middle.kind = bottom.kind = PMFieldKind::Check;
    top.key = "top"; middle.key = "middle"; bottom.key = "bottom";
    middle.enableKey = "top"; middle.enableVals = "true";
    bottom.enableKey = "middle"; bottom.enableVals = "true";
    std::vector<PMField> chain(3);
    initField(chain[0], top, false); initField(chain[1], middle, false);
    initField(chain[2], bottom, false);
    chain[1].checkValue = true;
    assert(!fieldVisible(bottom, chain));
    chain[0].checkValue = true;
    assert(fieldVisible(bottom, chain));


    PMFieldDef a, b;
    a.kind = b.kind = PMFieldKind::Check;
    a.key = "a"; a.enableKey = "b"; a.enableVals = "true";
    b.key = "b"; b.enableKey = "a"; b.enableVals = "true";
    std::vector<PMField> cycle(2);
    initField(cycle[0], a, false); initField(cycle[1], b, false);
    cycle[0].checkValue = cycle[1].checkValue = true;
    assert(!fieldVisible(a, cycle) && !fieldVisible(b, cycle));
}

void edit_patch_removes_only_form_owned_inactive_values() {
    PropertyManager hole; Access::open(hole, "hole", true);
    Access::load(hole, {{"params", {{"standard", "custom"},
                                   {"diameter", {{"value", 8.0}, {"expr", nullptr}}},
                                   {"other", 7}}}});
    choose(Access::field(hole, "standard", PMFieldKind::Combo), "M3");
    auto op = Access::feature(hole);
    // Null is the edit_feature deletion token; unrelated keys must not appear in the patch.
    assert(op.at("params").at("diameter").is_null());
    assert(op.at("params").at("standard") == "M3" && !op.at("params").contains("other"));
    choose(Access::field(hole, "standard", PMFieldKind::Combo), "custom");
    assert(Access::feature(hole).at("params").at("diameter").at("value") == 8.0);

    PropertyManager thread; Access::open(thread, "thread", true);
    Access::load(thread, {{"params", {{"size", "M8"}, {"preview", true},
                                      {"turns", "5"}, {"other", 7}}},
                          {"refs", {{"from", {{"kind", "edge"}}}, {"external", "keep"}}}});
    Access::field(thread, "custom_size", PMFieldKind::Check).checkValue = true;
    auto& target = Access::field(thread, "target_face", PMFieldKind::SelectorWell);
    pick(target);
    auto& from = Access::field(thread, "from", PMFieldKind::SelectorWell);
    assert(from.picks.size() == 1);
    from.picks.clear();
    Access::field(thread, "turns", PMFieldKind::Text).textBuf[0] = '\0';
    op = Access::feature(thread, true);
    const auto& params = op.at("params");
    const auto& refs = op.at("refs");
    assert(params.at("turns").is_null() && params.at("size").is_null());
    assert(params.at("preview").is_null() && params.at("length").is_null());
    assert(params.at("major_dia").at("value") == 12.0);
    assert(params.at("pitch").at("value") == 1.75 && !params.contains("other"));
    assert(refs.at("from").is_null() && !refs.contains("external"));
    assert(refs.at("target_face") == json({{"kind", "face"}}));
    // Reopening a saved custom thread recovers the UI controllers from real parameters.
    PropertyManager reopened; Access::open(reopened, "thread", true);
    Access::load(reopened, {{"params", {{"major_dia", {{"value", 12.0}, {"expr", nullptr}}},
                                       {"pitch", {{"value", 1.75}, {"expr", nullptr}}},
                                       {"length", {{"value", 4.0}, {"expr", nullptr}}}}},
                            {"refs", {{"target_face", {{"kind", "face"}}}}}});
    assert(Access::field(reopened, "custom_size", PMFieldKind::Check).checkValue);
    assert(!Access::field(reopened, "full_length", PMFieldKind::Check).checkValue);
    assert(Access::field(reopened, "length", PMFieldKind::Dim).dimValue == 4.0);
    auto draft = Access::feature(thread, false);
    assert(draft["params"]["preview"] == true);

    PropertyManager pattern; Access::open(pattern, "linear_pattern", true);
    Access::load(pattern, {{"params", {{"skip", "2:1"}, {"other", 9}}},
                           {"refs", {{"dir1", "axis.x"}}}});
    op = Access::feature(pattern);
    assert(op["refs"]["dir1"] == "axis.x"); // an empty same-key well must not delete the datum
    PropertyManager reopenedPattern; Access::open(reopenedPattern, "linear_pattern", true);
    Access::load(reopenedPattern, {{"params", json::object()}, {"refs", {{"dir1", "axis.x"}}}});
    assert(Access::field(reopenedPattern, "dir1", PMFieldKind::SelectorWell).picks.empty());
    assert(!op.at("params").contains("skip") && !op.at("params").contains("other"));

    PropertyManager newThread; Access::open(newThread, "thread");
    pick(Access::field(newThread, "target_face", PMFieldKind::SelectorWell));
    const auto finalThread = Access::feature(newThread, true);
    assert(finalThread.at("params").at("size") == "M3");
    assert(!finalThread.at("params").contains("preview"));
    assert(!finalThread.at("params").contains("length"));
    assert(!finalThread.at("refs").contains("from"));
    assert(Access::feature(newThread, false).at("params").at("preview") == true);

    PropertyManager add; Access::open(add, "hole");
    op = Access::feature(add);
    assert(!op["params"].contains("diameter")); // no null deletion on add
}

void fillet_expression_evidence_preserves_authored_values() {
    const json endpoint = {{"value", 7.0}, {"expr", "EndRadius"}};
    PropertyManager legacy; Access::open(legacy, "fillet", true);
    Access::load(legacy, {{"params", {{"radius", 2.0}, {"radius2", endpoint}}},
                          {"data", {{"fillet_effective",
                                     {{"effective", {{"radius", 2.0}, {"radius2", nullptr}}}}}}}});
    auto& radius2 = Access::field(legacy, "radius2", PMFieldKind::Dim);
    assert(!Access::visible(legacy, radius2));
    auto patch = Access::feature(legacy).at("params");
    assert(!patch.contains("type") && !patch.contains("radius2"));
    Access::hydrateFillet(legacy, {{"effective", {{"radius", 2.0}, {"radius2", 1.25}}}});
    assert(Access::visible(legacy, radius2) && radius2.dimValue == 1.25);
    patch = Access::feature(legacy).at("params");
    assert(!patch.contains("type") && patch.at("radius2") == endpoint);

    PropertyManager unevaluated; Access::open(unevaluated, "fillet", true);
    Access::load(unevaluated, {{"params", {{"radius", 2.0}, {"radius2", endpoint}}}});
    assert(Access::visible(unevaluated, Access::field(unevaluated, "radius2", PMFieldKind::Dim)));
    assert(Access::feature(unevaluated).at("params").at("radius2") == endpoint);

    const json authoredRadius = {{"value", 100.0}, {"expr", "Radius"}};
    PropertyManager typed; Access::open(typed, "fillet", true);
    Access::load(typed, {{"params", {{"type", "variable"}, {"radius", authoredRadius},
                                     {"radius2", endpoint}}},
                         {"data", {{"fillet_effective",
                                    {{"effective", {{"radius", 3.5}, {"radius2", 1.25}}}}}}}});
    assert(Access::field(typed, "radius", PMFieldKind::Dim).dimValue == 3.5);
    assert(Access::field(typed, "radius2", PMFieldKind::Dim).dimValue == 1.25);
    patch = Access::feature(typed).at("params");
    assert(patch.at("radius") == authoredRadius && patch.at("radius2") == endpoint);

    PropertyManager raw; Access::open(raw, "fillet", true);
    Access::load(raw, {{"params", {{"radius", "Radius"}}}});
    auto& rawRadius = Access::field(raw, "radius", PMFieldKind::Dim);
    assert(!rawRadius.dimValueKnown && Access::feature(raw).at("params").at("radius") == "Radius");
    Access::hydrateFillet(raw, {{"effective", {{"radius", 3.5}, {"radius2", nullptr}}}});
    assert(rawRadius.dimValueKnown && rawRadius.dimValue == 3.5);
    assert(Access::feature(raw).at("params").at("radius") == "Radius");
}

void unit_changes_preserve_invalid_and_uncommitted_text() {
    PropertyManager box; Access::open(box, "box", false, false);
    auto& length = Access::field(box, "length", PMFieldKind::Dim);
    text(length, "bad", false); assert(!parseFieldText(length, false));
    Access::units(box, true);
    assert(std::string(length.dimBuf.data()) == "bad" && !length.dimError.empty());
    text(length, "2", false); // edited while in MMGS, then units changed
    Access::units(box, false); Access::units(box, true);
    assert(std::string(length.dimBuf.data()) == "2");
    assert(parseFieldText(length, length.dimInputIps) && length.dimValue == 2.0);
    text(length, "=width/2", true);
    assert(parseFieldText(length, true));
    Access::units(box, false);
    assert(std::string(length.dimBuf.data()) == "=width/2" && length.dimExpr == "width/2");

    PropertyManager displayed; Access::open(displayed, "box", false, false);
    auto& shown = Access::field(displayed, "length", PMFieldKind::Dim);
    text(shown, "25.4mm", false);
    assert(parseFieldText(shown, false));
    Access::units(displayed, true);
    assert(std::string(shown.dimBuf.data()) == "1in" && shown.dimValue == 25.4);
    shown.dimTextEditing = true;
    std::snprintf(shown.dimBuf.data(), shown.dimBuf.size(), "1.00");
    Access::units(displayed, false);
    assert(std::string(shown.dimBuf.data()) == "1.00" && shown.dimInputIps);

    PropertyManager focused; Access::open(focused, "box", false, false);
    auto& pending = Access::field(focused, "length", PMFieldKind::Dim);
    text(pending, "2", false);
    pending.dimTextEditing = true;
    Access::units(focused, true);
    assert(Access::flush(focused));
    assert(pending.dimValue == 2.0 && pending.dimSyncPending && !pending.dimInputIps);
    assert(std::string(pending.dimBuf.data()) == "2");
    pending.dimTextEditing = false;
    syncAcceptedDimText(pending, true);
    double value = 0.0; std::string error;
    assert(pending.dimInputIps && pmParseDim(pending.dimBuf.data(), value, error, false, 25.4));
    assert(std::abs(value - 2.0) < 1e-4);
}
}

int main() {
    parse_units_and_boundaries();
    pending_text_flushes_into_the_feature();
    readiness_tracks_all_visible_fields_and_references();
    visibility_is_transitive_and_cycle_safe();
    edit_patch_removes_only_form_owned_inactive_values();
    fillet_expression_evidence_preserves_authored_values();
    unit_changes_preserve_invalid_and_uncommitted_text();
    return 0;
}
