// Drawing provenance (core/doc/drawing.cpp hlr_project): every projected line and circle in
// drawing_get names the model edge it was drawn from — body ordinal, TopExp edge id and the
// stable sid — in exactly doc_query's numbering, so an agent holding a sid can find its
// projection. Built on a 100x10x40 upright plate with four identical Ø6 holes drilled along Y:
// the four holes project as four EQUAL circles in the front view, the case a size match cannot
// tell apart.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <atomic>
#include <array>
#include <cmath>
#include <chrono>
#include <exception>
#include <filesystem>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/drawing_session.h"
#include "doc/session.h"
#include "doc/stepplus.h"

using json = nlohmann::json;
namespace doc = chudware::doc;

namespace {
json dimv(double v) { return json{{"value", v}, {"expr", nullptr}}; }

constexpr std::array<double, 4> kHoleX = {-36.0, -12.0, 12.0, 36.0};

std::string make_cross_hole_plate() {
    json nr = doc::doc_new("part", "CrossHolePlate");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(100)}, {"width", dimv(10)}, {"height", dimv(40)}}}}}},
    });
    for (std::size_t i = 0; i < kHoleX.size(); ++i)
        ops.push_back(json{{"op", "add_feature"},
                           {"feature", {{"type", "cylinder"}, {"name", "Hole" + std::to_string(i + 1)},
                                        {"params", {{"radius", dimv(3)}, {"height", dimv(20)},
                                                    {"at", json::array({kHoleX[i], 0.0, 0.0})},
                                                    {"axis", json::array({0, 1, 0})},
                                                    {"mode", "cut"}}}}}});
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// Returns a pointer INTO `full`: iterate the stored array by reference, never a
// json::value() copy (a pointer into that temporary dangles once the loop ends).
const json* find_view(const json& full, const std::string& view_id) {
    for (const json& sheet : full.at("sheets")) {
        if (!sheet.contains("views")) continue;
        for (const json& view : sheet.at("views"))
            if (view.value("view_id", std::string()) == view_id) return &view;
    }
    return nullptr;
}
}  // namespace

TEST_CASE("drawing_get carries body + TopExp edge id + doc_query sid on every projected edge") {
    const std::string src = make_cross_hole_plate();
    json created = doc::drawing_new(json::object(), json::object(), "Provenance", src);
    const std::string did = created.at("doc_id").get<std::string>();
    const double px = 150.0, py = 150.0;
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({px, py}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    // doc_query's own numbering: (body, topo) -> sid for every edge, and the four hole walls.
    std::map<std::pair<int, int>, std::string> edge_sid;
    // Bind the reply first: ranging over doc_query(...).at(...) would iterate a reference into
    // a temporary that dies before the loop body runs.
    const json edges = doc::doc_query(src, json{{"of", "edge"}});
    for (const json& m : edges.at("matches"))
        edge_sid[{m.at("body").get<int>(), m.at("topo").get<int>()}] = m.at("sid").get<std::string>();
    json walls = doc::doc_query(src, json{{"of", "face"}, {"surf", "cylinder"}}).at("matches");
    REQUIRE(walls.size() == kHoleX.size());
    std::vector<std::pair<double, std::string>> wall_sids;
    for (const json& m : walls)
        wall_sids.emplace_back(m.at("centroid")[0].get<double>(), m.at("sid").get<std::string>());
    std::sort(wall_sids.begin(), wall_sids.end());

    const json full = doc::drawing_get(did);
    const json* view = find_view(full, vid);
    REQUIRE(view != nullptr);

    // Every polyline has an aligned source record, and on this all-sharp-edge part every one
    // of them names a real model edge whose sid is the one doc_query hands out.
    for (const char* layer : {"visible", "hidden"}) {
        const json& pls = view->at(layer);
        const json& srcs = view->at(std::string(layer) + "_src");
        REQUIRE(srcs.size() == pls.size());
        for (const json& s : srcs) {
            REQUIRE(s.is_object());
            CHECK(s.at("kind") == "edge");
            const auto key = std::make_pair(s.at("body").get<int>(), s.at("edge_id").get<int>());
            REQUIRE(edge_sid.count(key) == 1);
            CHECK(s.at("sid") == edge_sid.at(key));
        }
    }

    // Each hole's wall sid reaches exactly one visible circle, centred on THAT hole's model x.
    const json& arcs = view->at("arcs");
    REQUIRE(arcs.size() == 2 * kHoleX.size());  // front rim + back rim per through hole
    for (std::size_t k = 0; k < kHoleX.size(); ++k) {
        const std::string& wall = wall_sids[k].second;
        std::set<std::pair<long, long>> centres;
        int visible = 0;
        for (const json& arc : arcs) {
            const json& s = arc.at("src");
            REQUIRE(s.is_object());
            const auto key = std::make_pair(s.at("body").get<int>(), s.at("edge_id").get<int>());
            CHECK(s.at("sid") == edge_sid.at(key));
            const json& faces = s.at("face_sids");
            if (std::find(faces.begin(), faces.end(), json(wall)) == faces.end()) continue;
            const double cx = arc.at("center")[0].get<double>();
            const double cy = arc.at("center")[1].get<double>();
            CHECK(std::fabs((cx - px) - kHoleX[k]) < 1e-6);
            CHECK(std::fabs(arc.at("radius").get<double>() - 3.0) < 1e-6);
            centres.insert({std::lround(cx * 1e4), std::lround(cy * 1e4)});
            if (arc.at("layer") == "visible") ++visible;
        }
        CHECK(centres.size() == 1);
        CHECK(visible == 1);
    }

    CHECK(doc::drawing_close(did));
    CHECK(doc::doc_close(src));
}

TEST_CASE("drawing diameter follows its model edge and reports deletion as stale") {
    const std::string src = make_cross_hole_plate();
    const json tree = doc::doc_tree(src).at("tree");
    const std::string hole = tree.at("features").at(1).at("id");
    const std::string did = doc::drawing_new(json::object(), json::object(), "Associative", src)
                                .at("doc_id");
    const std::string vid = doc::drawing_add_view(
        did, "base", "front", "s1", 1.0, json::array({150.0, 150.0}), nullptr).at("view_id");
    const json created = doc::drawing_add_dim(
        did, vid, "diameter_dim", json::array({117.0, 150.0}), nullptr, nullptr, nullptr,
        nullptr, nullptr);
    const std::string ann_id = created.at("annotation").at("id");
    auto annotation = [&](const json& drawing) -> json {
        const json* view = find_view(drawing, vid);
        if (!view) return nullptr;
        for (const auto& ann : view->at("annotations"))
            if (ann.value("id", std::string()) == ann_id) return ann;
        return nullptr;
    };
    json first = annotation(doc::drawing_get(did));
    REQUIRE(first.is_object());
    CHECK_FALSE(first.at("stale").get<bool>());
    CHECK(first.at("value").get<double>() == doctest::Approx(6.0));
    doc::doc_apply_ops(src, -1, json::array({
        json{{"op", "edit_feature"}, {"id", hole}, {"params", {{"radius", dimv(4)}}}}
    }), false);
    json edited = annotation(doc::drawing_get(did));
    REQUIRE(edited.is_object());
    CHECK_FALSE(edited.at("stale").get<bool>());
    CHECK(edited.at("value").get<double>() == doctest::Approx(8.0));
    doc::doc_apply_ops(src, -1, json::array({
        json{{"op", "delete_feature"}, {"id", hole}}
    }), false);
    json deleted = annotation(doc::drawing_get(did));
    REQUIRE(deleted.is_object());
    CHECK(deleted.at("stale").get<bool>());
    CHECK(deleted.contains("stale_reason"));
    CHECK(doc::drawing_close(did));
    CHECK(doc::doc_close(src));
}

TEST_CASE("a section view's cut pieces keep the source edge they were cut from") {
    const std::string src = make_cross_hole_plate();
    json created = doc::drawing_new(json::object(), json::object(), "Provenance Section", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({150.0, 150.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    // A horizontal cut through the hole axes (sheet y of the model's z = 0) halves every rim.
    json sec = doc::drawing_add_section(did, vid, json(nullptr),
                                        json::array({json::array({80.0, 150.0}),
                                                     json::array({220.0, 150.0})}),
                                        json(nullptr), json(nullptr), json::array({150.0, 60.0}),
                                        "s1", json(false), json::object());
    const std::string sid_view = sec.at("view_id").get<std::string>();

    std::set<std::string> edge_sids;
    const json edges = doc::doc_query(src, json{{"of", "edge"}});
    for (const json& m : edges.at("matches"))
        edge_sids.insert(m.at("sid").get<std::string>());

    const json full = doc::drawing_get(did);
    const json* view = find_view(full, sid_view);
    REQUIRE(view != nullptr);
    const json& srcs = view->at("visible_src");
    REQUIRE(srcs.size() == view->at("visible").size());
    int traced = 0;
    for (const json& s : srcs) {
        if (s.is_null()) continue;  // an edge the cut generated on its plane
        ++traced;
        CHECK(s.at("body") == 1);
        CHECK(edge_sids.count(s.at("sid").get<std::string>()) == 1);
    }
    CHECK(traced > 0);

    CHECK(doc::drawing_close(did));
    CHECK(doc::doc_close(src));
}

TEST_CASE("failed source rebuild cannot project an unconsumed tool as drawing geometry") {
    const std::string src = make_cross_hole_plate();
    const json valid = doc::doc_tree(src).at("tree");
    const std::string drawing = doc::drawing_new(json::object(), json::object(),
                                                  "Failed source", src).at("doc_id");
    const auto view = doc::drawing_add_view(drawing, "base", "front", "s1", json(1.0),
                                             json::array({150.0, 150.0}), json(nullptr));
    const json before = doc::drawing_get(drawing);
    json broken = valid;
    const std::string base = broken.at("features").at(0).at("id");
    broken["features"].push_back({
        {"id", "f90"}, {"type", "box"}, {"name", "StandaloneTool"}, {"suppressed", false},
        {"params", {{"length", 10.0}, {"width", 10.0}, {"height", 20.0},
                    {"at", {0, 0, 0}}, {"merge", false}}}, {"refs", json::object()}});
    broken["features"].push_back({
        {"id", "f91"}, {"type", "boolean"}, {"name", "FailedBoolean"}, {"suppressed", false},
        {"params", {{"op", "subtract"}}},
        {"refs", {{"targets", json::array({base})}, {"tools", json::array({"f999"})}}}});

    doc::drawing_set_source(drawing, broken, json::object());
    try {
        doc::drawing_get(drawing);
        FAIL("a failed source must not project its partial tool solids");
    } catch (const doc::OpError& error) {
        CHECK(error.code == "rebuild_failed");
        CHECK(error.detail.at("reason") == "feature_failed");
    }
    CHECK_THROWS_AS(doc::drawing_add_view(drawing, "base", "top", "s1", json(1.0),
                                          json::array({100.0, 100.0}), json(nullptr)),
                    doc::OpError);
    CHECK_THROWS_AS(doc::drawing_set_view(drawing, view.at("view_id"), json(2.0),
                                          json(nullptr), json(nullptr), json(nullptr)),
                    doc::OpError);
    // Failed projection/edit attempts leave existing views and revision untouched;
    // restoring a complete source gives back the same projection.
    doc::drawing_set_source(drawing, valid, json::object());
    const json after = doc::drawing_get(drawing);
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(after.at("tree").at("sheets") == before.at("tree").at("sheets"));
    const std::string view_id = view.at("view_id");
    const json* prior_view = find_view(before, view_id);
    const json* restored_view = find_view(after, view_id);
    REQUIRE(prior_view != nullptr);
    REQUIRE(restored_view != nullptr);
    for (const char* layer : {"visible", "hidden", "arcs"})
        CHECK(restored_view->at(layer) == prior_view->at(layer));
    CHECK(doc::drawing_close(drawing));
    CHECK(doc::doc_close(src));
}

namespace {
namespace fs = std::filesystem;

struct HistoryDrawingSource {
    std::string id;
    std::string datum;
    json built;
    fs::path archive;
    fs::path directory;
    std::vector<std::string> drawings;

    HistoryDrawingSource() {
        id = doc::doc_new("part", "DrawingHistorySource").at("doc_id").get<std::string>();
        built = doc::doc_apply_ops(id, -1, json::array({
            json{{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Base"},
                {"params", {{"length", dimv(20)}, {"width", dimv(20)}, {"height", dimv(10)}}}}}},
            json{{"op", "add_feature"}, {"feature", {
                {"type", "plane"}, {"name", "UnusedDatum"},
                {"params", {{"method", "offset"}, {"offset", 10.0}}},
                {"refs", {{"base", "plane.top"}}}}}}
        }), false);
        datum = built.at("id_map").at("$1").get<std::string>();
        doc::doc_clear_history(id);
        const std::string unique = id + "-" + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count());
        archive = fs::temp_directory_path() / ("drawing-history-" + unique + ".draw+");
        directory = fs::temp_directory_path() / ("drawing-history-" + unique + "-directory");
    }

    ~HistoryDrawingSource() {
        for (const auto& drawing : drawings) {
            try { doc::drawing_close(drawing); } catch (...) {}
        }
        try { doc::doc_close(id); } catch (...) {}
        std::error_code ec;
        fs::remove(archive, ec);
        fs::remove(directory, ec);
    }
};

bool has_history_fix(const json& report, const char* list, const std::string& id) {
    for (const auto& fix : report.at(list))
        for (const auto& feature : fix.at("feature_ids"))
            if (feature == id) return true;
    return false;
}

bool history_safe(const HistoryDrawingSource& source) {
    return has_history_fix(doc::doc_eval_model_history(source.id), "safe_fixes", source.datum);
}

bool source_dirty(const std::string& id) {
    const json docs = doc::doc_list();
    for (const auto& entry : docs.at("docs"))
        if (entry.at("doc_id") == id) return entry.at("dirty").get<bool>();
    throw std::runtime_error("source document is not open");
}
}  // namespace

TEST_CASE("live and saved standalone drawings protect their source history") {
    HistoryDrawingSource source;
    const json before = doc::doc_tree(source.id);
    const long revision = before.at("revision").get<long>();
    const bool dirty = source_dirty(source.id);
    REQUIRE(history_safe(source));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    CHECK_THROWS(doc::doc_undo(source.id));

    // Both fail after a source was associated, without leaving an invisible drawing.
    CHECK_THROWS(doc::register_restored_drawing(
        json{{"schema", "aicad.drawing/1"}, {"name", 17}, {"sheets", json::array()}},
        source.id));
    CHECK_THROWS(doc::drawing_make(source.id, json{{"projection", 17}}));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    REQUIRE(history_safe(source));

    const json created =
        doc::drawing_new(json::object(), json::object(), "Source drawing", source.id);
    const std::string drawing = created.at("doc_id").get<std::string>();
    source.drawings.push_back(drawing);
    const json dependents = doc::doc_dep_query(source.id, "drawing").at("dependents");
    REQUIRE(dependents.size() == 1);
    CHECK(dependents[0].at("dep_doc_id") == drawing);
    CHECK(dependents[0].at("linked_revision") == revision);
    CHECK_FALSE(history_safe(source));
    CHECK_THROWS(doc::doc_apply_ops(source.id, revision, json::array({
        {{"op", "simplify_history"}, {"features", json::array({source.datum})}}
    }), false));
    CHECK(doc::doc_tree(source.id).at("tree") == before.at("tree"));
    CHECK(doc::doc_tree(source.id).at("revision") == revision);
    CHECK_THROWS(doc::doc_undo(source.id));

    const json saved = doc::drawing_save_standalone(drawing, source.archive.string());
    CHECK(saved.at("path") == source.archive.string());
    const auto archive =
        doc::editable_container_open(source.archive.string(), "aicad.draw+/", ".draw+");
    REQUIRE(archive.extra.count("source/document.json") == 1);
    const json embedded = json::parse(archive.extra.at("source/document.json"));
    CHECK(embedded.at("features") == before.at("tree").at("features"));
    CHECK(doc::doc_live_source(source.id).at("path").is_null());
    CHECK(source_dirty(source.id) == dirty);
    CHECK(doc::doc_tree(source.id).at("tree") == before.at("tree"));
    CHECK(doc::doc_tree(source.id).at("revision") == revision);

    CHECK(doc::drawing_close(drawing));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    const json published_report = doc::doc_eval_model_history(source.id);
    CHECK_FALSE(has_history_fix(published_report, "safe_fixes", source.datum));
    CHECK(has_history_fix(published_report, "blocked_fixes", source.datum));
    CHECK_THROWS(doc::doc_apply_ops(source.id, revision, json::array({
        {{"op", "simplify_history"}, {"features", json::array({source.datum})}}
    }), false));
    const json no_op = doc::doc_apply_ops(source.id, revision,
        json::array({{{"op", "simplify_history"}}}), false);
    CHECK(no_op.at("tree") == before.at("tree"));
    CHECK(no_op.at("revision") == revision);
    CHECK(doc::doc_tree(source.id).at("tree") == before.at("tree"));
    CHECK(doc::doc_tree(source.id).at("revision") == revision);
    CHECK(source_dirty(source.id) == dirty);
    CHECK_THROWS(doc::doc_undo(source.id));

    // Reopening without a saved .step+ creates an already-public embedded source.
    const json opened = doc::drawing_open_standalone(source.archive.string());
    CHECK(opened.at("source_state") == "embedded_opened");
    const std::string reopened_drawing = opened.at("drawing_id").get<std::string>();
    const std::string reopened_source = opened.at("source_doc_id").get<std::string>();
    source.drawings.push_back(reopened_drawing);
    CHECK_FALSE(has_history_fix(doc::doc_eval_model_history(reopened_source),
                                "safe_fixes", source.datum));
    CHECK(doc::doc_dep_query(reopened_source, "drawing").at("dependents").size() == 1);
    CHECK(doc::drawing_close(reopened_drawing));
    CHECK(doc::doc_live_source(reopened_source).is_null());
    CHECK(doc::doc_dep_query(reopened_source, "drawing").at("dependents").empty());

    const json reopened = doc::drawing_open_standalone(source.archive.string());
    const std::string replacement_drawing = reopened.at("drawing_id").get<std::string>();
    const std::string replaced_source = reopened.at("source_doc_id").get<std::string>();
    source.drawings.push_back(replacement_drawing);
    doc::drawing_set_source(replacement_drawing, before.at("tree"), json::object());
    CHECK(doc::doc_live_source(replaced_source).is_null());
    CHECK(doc::doc_dep_query(replaced_source, "drawing").at("dependents").empty());
    CHECK(doc::drawing_close(replacement_drawing));
}

TEST_CASE("failed standalone save does not publish source history") {
    HistoryDrawingSource source;
    REQUIRE(history_safe(source));
    const std::string drawing =
        doc::drawing_new(json::object(), json::object(), "", source.id)
            .at("doc_id").get<std::string>();
    source.drawings.push_back(drawing);
    CHECK_FALSE(history_safe(source));
    const json before = doc::drawing_get(drawing);
    fs::create_directories(source.directory);
    CHECK_THROWS(doc::drawing_save_standalone(drawing, source.directory.string()));
    const json after = doc::drawing_get(drawing);
    CHECK(after.at("tree") == before.at("tree"));
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").size() == 1);
    CHECK(doc::drawing_close(drawing));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    CHECK(history_safe(source));
    CHECK(doc::doc_live_source(source.id).at("path").is_null());
    CHECK_THROWS(doc::doc_undo(source.id));
}

TEST_CASE("drawing source replacement, restoration and source close balance dependencies") {
    HistoryDrawingSource source;
    const json created =
        doc::drawing_new(json::object(), json::object(), "", source.id);
    const std::string drawing = created.at("doc_id").get<std::string>();
    source.drawings.push_back(drawing);
    CHECK_FALSE(history_safe(source));

    const std::string other =
        doc::doc_new("part", "OtherDrawingSource").at("doc_id").get<std::string>();
    const json other_tree = doc::doc_tree(other).at("tree");
    CHECK(doc::doc_close(other));
    doc::drawing_set_source(drawing, other_tree, json::object());
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    REQUIRE(history_safe(source));

    const std::string restored =
        doc::register_restored_drawing(created.at("drawing"), source.id)
            .at("doc_id").get<std::string>();
    source.drawings.push_back(restored);
    CHECK_FALSE(history_safe(source));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").size() == 1);
    CHECK(doc::drawing_close(restored));
    REQUIRE(history_safe(source));

    const std::string closing =
        doc::register_restored_drawing(created.at("drawing"), source.id)
            .at("doc_id").get<std::string>();
    source.drawings.push_back(closing);
    CHECK(doc::doc_close(source.id));
    CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
    const json frozen = doc::drawing_get(closing);
    CHECK(frozen.at("source_state") == "closed");
    CHECK(frozen.at("source_missing") == true);
    CHECK(doc::drawing_close(closing));
    CHECK(doc::drawing_close(drawing));
}

TEST_CASE("racing standalone save and drawing close cannot expose saved source history") {
    for (int attempt = 0; attempt < 4; ++attempt) {
        HistoryDrawingSource source;
        const std::string drawing =
            doc::drawing_new(json::object(), json::object(), "", source.id)
                .at("doc_id").get<std::string>();
        source.drawings.push_back(drawing);
        const long revision = source.built.at("revision").get<long>();
        const json cleanup_ops = json::array({
            {{"op", "simplify_history"}, {"features", json::array({source.datum})}}
        });
        std::atomic<bool> start{false};
        bool saved = false;
        bool closed = false;
        bool cleaned = false;
        std::exception_ptr save_error, close_error, cleanup_error;
        std::thread saver([&] {
            while (!start.load(std::memory_order_acquire)) std::this_thread::yield();
            try {
                doc::drawing_save_standalone(drawing, source.archive.string());
                saved = true;
            } catch (...) {
                save_error = std::current_exception();
            }
        });
        std::thread closer([&] {
            while (!start.load(std::memory_order_acquire)) std::this_thread::yield();
            try {
                closed = doc::drawing_close(drawing);
            } catch (...) {
                close_error = std::current_exception();
            }
        });
        std::thread cleaner([&] {
            while (!start.load(std::memory_order_acquire)) std::this_thread::yield();
            try {
                doc::doc_apply_ops(source.id, revision, cleanup_ops, false);
                cleaned = true;
            } catch (...) {
                cleanup_error = std::current_exception();
            }
        });
        start.store(true, std::memory_order_release);
        saver.join();
        closer.join();
        cleaner.join();
        CHECK_FALSE(close_error);
        CHECK(closed);
        if (save_error) {
            try {
                std::rethrow_exception(save_error);
            } catch (const doc::OpError& error) {
                CHECK(error.code == "unknown_doc");
            } catch (...) {
                FAIL("unexpected error from racing drawing save");
            }
        }
        if (cleanup_error) {
            try {
                std::rethrow_exception(cleanup_error);
            } catch (const doc::OpError& error) {
                CHECK(error.code == "invalid_op");
            } catch (...) {
                FAIL("unexpected error from racing history cleanup");
            }
        }
        if (saved) CHECK_FALSE(cleaned);
        CHECK(doc::doc_dep_query(source.id, "drawing").at("dependents").empty());
        CHECK(history_safe(source) == (!saved && !cleaned));
    }
}
