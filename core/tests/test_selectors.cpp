// Selector system verification — the foundation pattern/mirror/hole/fillet-by-pick
// features stand on. Two groups, mirroring test_document.cpp's doctest style:
//
//   GROUP A (pure resolution, hand-built TopoInfo): the §3 scoring + §5 torture
//   obligations. These assertions ARE the product — they pin the EXACT accept/
//   reject behaviour: the 0.35 score floor, the 0.05 symmetric-ambiguity margin
//   that REFUSES to guess, the hint_index tiebreak, and the owner filter+fallback.
//   Scores are computed by hand (see the comment on each case) so the cases land
//   deliberately on the right side of each threshold. Vertices are used where pure
//   centroid-distance control is wanted: with surf/axis/measure all null the score
//   collapses to 0.55*dc, so dc = |dcentroid|/bbox_diag drives everything.
//
//   GROUP B (OCCT describers + end-to-end): describe_faces/edges/vertices over a
//   real box + cylinder (analytic, OCCT-version-independent invariants), then the
//   whole point of the module — make_selector from a face and resolve it back,
//   INCLUDING across a dimension change (the §5 "survives a rebuild" spirit).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <map>
#include <limits>
#include <optional>
#include <string>
#include <vector>
#include <utility>

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepPrimAPI_MakeCone.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepPrimAPI_MakeTorus.hxx>
#include <BRep_Builder.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include "doc/selectors.h"
#include "geom/features.h"

using chudware::doc::TopoInfo;
using chudware::doc::ResolveResult;
using chudware::doc::make_selector;
using chudware::doc::resolve;
using json = nlohmann::json;

namespace {

// Build a vertex TopoInfo (surf/axis/measure all null -> score is purely 0.55*dc).
TopoInfo vtx(int index, double x, double y, double z, int hint = -1,
             std::optional<std::string> owner = std::nullopt) {
    TopoInfo t;
    t.kind = "vertex";
    t.index = index;
    t.surf = std::nullopt;
    t.centroid = Eigen::Vector3d(x, y, z);
    t.axis = std::nullopt;
    t.measure = std::nullopt;
    t.owner = owner;
    t.hint_index = (hint < 0) ? index : hint;
    return t;
}

// A vertex selector dict at a given point (no axis/measure/surf terms).
json vtx_selector(double x, double y, double z,
                  std::optional<std::string> owner = std::nullopt) {
    json s;
    s["kind"] = "vertex";
    s["surf"] = nullptr;
    s["centroid"] = json::array({x, y, z});
    s["axis"] = nullptr;
    s["area_or_len"] = nullptr;
    s["owner_feature"] = owner ? json(*owner) : json(nullptr);
    s["hint_index"] = 0;
    return s;
}

}  // namespace

// ===========================================================================
// GROUP A — pure resolution (the §3 + §5 obligations)
// ===========================================================================

// A1. CLEAN ACCEPT. bbox_diag=100, selector at origin.
//   A=(1,0,0):  dc=0.01  -> score 0.0055   (best)
//   B=(50,0,0): dc=0.50  -> score 0.2750   (second)
//   C=(60,0,0): dc=0.60  -> score 0.3300
//   best 0.0055 < 0.35 AND margin 0.2695 > 0.05  -> ACCEPT A (index 10).
TEST_CASE("A1 clean accept: the one clearly-closest candidate wins") {
    std::vector<TopoInfo> cands = {
        vtx(10, 1, 0, 0),
        vtx(20, 50, 0, 0),
        vtx(30, 60, 0, 0),
    };
    ResolveResult r = resolve(vtx_selector(0, 0, 0), cands, 100.0);
    REQUIRE(r.index.has_value());
    CHECK(*r.index == 10);
    CHECK(r.score == doctest::Approx(0.0055));
    CHECK(r.second_score == doctest::Approx(0.275));
    CHECK_FALSE(r.dangling_reason.has_value());
}

// A2. DANGLING BY HIGH SCORE. selector at origin, every candidate far.
//   A=(70,0,0): dc=0.70 -> score 0.3850   (>= 0.35)
//   B=(80,0,0): dc=0.80 -> score 0.4400
//   best 0.3850 >= 0.35 -> DANGLING, reason mentions ">= 0.35".
TEST_CASE("A2 dangling: best score over the 0.35 floor") {
    std::vector<TopoInfo> cands = {
        vtx(1, 70, 0, 0),
        vtx(2, 80, 0, 0),
    };
    ResolveResult r = resolve(vtx_selector(0, 0, 0), cands, 100.0);
    CHECK_FALSE(r.index.has_value());
    CHECK(r.score == doctest::Approx(0.385));
    REQUIRE(r.dangling_reason.has_value());
    CHECK(r.dangling_reason->find(">= 0.35") != std::string::npos);
    MESSAGE("A2 reason: " << *r.dangling_reason);
}

// A3. SYMMETRIC AMBIGUITY (§5). Two candidates EQUIDISTANT from the selector.
//   A=(10,0,0):  dc=0.10 -> score 0.0550
//   B=(-10,0,0): dc=0.10 -> score 0.0550
//   best 0.0550 < 0.35 but margin 0.0000 <= 0.05 -> the margin rule REFUSES to
//   guess. DANGLING, reason mentions "margin". (The whole point of §5: never a
//   silent wrong pick between symmetric faces.)
TEST_CASE("A3 symmetric ambiguity: margin rule forces dangling, no guess") {
    std::vector<TopoInfo> cands = {
        vtx(7, 10, 0, 0),
        vtx(8, -10, 0, 0),
    };
    ResolveResult r = resolve(vtx_selector(0, 0, 0), cands, 100.0);
    CHECK_FALSE(r.index.has_value());
    CHECK(r.score == doctest::Approx(0.055));
    CHECK(r.second_score == doctest::Approx(0.055));
    REQUIRE(r.dangling_reason.has_value());
    CHECK(r.dangling_reason->find("margin") != std::string::npos);
    MESSAGE("A3 reason: " << *r.dangling_reason
            << "  (best=" << r.score << " second=" << r.second_score << ")");
}

// A4. hint_index TIEBREAK. Two candidates with IDENTICAL score but different
//   hint_index, plus a third worse one. The sort key is (score, hint_index), so the
//   nearer (smaller) hint_index becomes best_cand; the equal-score sibling becomes
//   second_cand. Because the tied pair occupies the top two slots the margin is 0
//   and the margin rule correctly DANGLES (a tie is genuine ambiguity) — but the
//   TIEBREAK is observable: best_cand is the low-hint one, so r.score is that pair's
//   score and the dangling reason is the symmetric-"margin" form, NOT the ">=0.35"
//   form. This pins the (score, hint_index) ordering.
//     A=(10,0,0) hint=5: dc=0.10 -> 0.0550
//     B=(0,10,0) hint=2: dc=0.10 -> 0.0550   (lower hint -> sorts first = best)
//     C=(40,0,0) hint=9: dc=0.40 -> 0.2200   (the worse third)
TEST_CASE("A4 hint_index tiebreak: lower hint sorts first (margin still dangles a true tie)") {
    std::vector<TopoInfo> cands = {
        vtx(/*index*/ 50, 10, 0, 0, /*hint*/ 5),
        vtx(/*index*/ 60, 0, 10, 0, /*hint*/ 2),
        vtx(/*index*/ 70, 40, 0, 0, /*hint*/ 9),
    };
    ResolveResult r = resolve(vtx_selector(0, 0, 0), cands, 100.0);
    // Genuine tie at the top -> dangling by the margin rule (never guess on a tie).
    CHECK_FALSE(r.index.has_value());
    CHECK(r.score == doctest::Approx(0.055));        // the tied pair's score is on top
    CHECK(r.second_score == doctest::Approx(0.055));  // C (0.22) is NOT in the top two
    REQUIRE(r.dangling_reason.has_value());
    CHECK(r.dangling_reason->find("margin") != std::string::npos);

    // Now BREAK the tie so the lower-hint candidate is the strict best and the
    // margin opens wide against the others. The accepted index must be B's
    // (index 60) — confirming the low-hint B was the one carried to the top.
    std::vector<TopoInfo> cands2 = {
        vtx(/*index*/ 50, 40, 0, 0, /*hint*/ 5),     // dc=0.40 -> 0.2200
        vtx(/*index*/ 60, 2, 0, 0,  /*hint*/ 2),     // dc=0.02 -> 0.0110 (strict best)
        vtx(/*index*/ 70, 60, 0, 0, /*hint*/ 9),     // dc=0.60 -> 0.3300
    };
    ResolveResult r2 = resolve(vtx_selector(0, 0, 0), cands2, 100.0);
    REQUIRE(r2.index.has_value());     // best 0.0110 < 0.35, margin 0.2090 > 0.05
    CHECK(*r2.index == 60);
    CHECK(r2.score == doctest::Approx(0.011));
    CHECK(r2.second_score == doctest::Approx(0.22));
}

// A5. OWNER FILTER + FALLBACK. Candidates tagged with owners.
//   (a) selector owner=="fX" restricts to fX candidates EVEN WHEN a different-owner
//       candidate is geometrically closer. The closest fX candidate is chosen.
//   (b) when NO candidate carries the selector's owner, the resolver falls back to
//       ALL same-kind candidates and picks the geometrically closest.
TEST_CASE("A5 owner filter restricts; empty owner set falls back to all same-kind") {
    // (a) The geometrically closest vertex (index 1) belongs to owner "fY"; the
    //     selector wants owner "fX". Owner filter must IGNORE the closer fY vertex
    //     and resolve to the closest fX vertex (index 3).
    std::vector<TopoInfo> cands = {
        vtx(1, 1, 0, 0,  /*hint*/ 1, std::string("fY")),  // closest overall, wrong owner
        vtx(2, 90, 0, 0, /*hint*/ 2, std::string("fX")),  // fX but far
        vtx(3, 11, 0, 0, /*hint*/ 3, std::string("fX")),  // fX, the intended pick
    };
    ResolveResult r = resolve(vtx_selector(0, 0, 0, std::string("fX")), cands, 100.0);
    REQUIRE(r.index.has_value());
    CHECK(*r.index == 3);  // restricted to fX; closer fY (index 1) ignored
    // Within fX {index2 @0.4950, index3 @0.0605}: best 0.0605 < 0.35, margin 0.4345 > 0.05.
    CHECK(r.score == doctest::Approx(0.0605));

    // (b) Selector wants owner "fZ" which NO candidate has -> fall back to all
    //     same-kind and pick the geometrically closest. Over all 3:
    //     index1 @0.0055 (best), index3 @0.0605 (second) -> margin 0.0550 > 0.05.
    ResolveResult rb = resolve(vtx_selector(0, 0, 0, std::string("fZ")), cands, 100.0);
    REQUIRE(rb.index.has_value());
    CHECK(*rb.index == 1);
    CHECK(rb.score == doctest::Approx(0.0055));
    CHECK(rb.second_score == doctest::Approx(0.0605));
}

// A6. NO CANDIDATES OF THE SELECTOR'S KIND. A face selector against only vertices
//   -> dangling with the exact "no candidates of matching kind" reason.
TEST_CASE("A6 no matching kind: dangling with the matching-kind reason") {
    std::vector<TopoInfo> cands = {vtx(1, 0, 0, 0), vtx(2, 1, 1, 1)};
    json face_sel;
    face_sel["kind"] = "face";
    face_sel["surf"] = "plane";
    face_sel["centroid"] = json::array({0, 0, 0});
    face_sel["axis"] = json::array({0, 0, 1});
    face_sel["area_or_len"] = 10.0;
    face_sel["owner_feature"] = nullptr;
    face_sel["hint_index"] = 0;

    ResolveResult r = resolve(face_sel, cands, 100.0);
    CHECK_FALSE(r.index.has_value());
    CHECK(std::isinf(r.score));
    REQUIRE(r.dangling_reason.has_value());
    CHECK(*r.dangling_reason == "no candidates of matching kind");
}

// ===========================================================================
// GROUP B — OCCT describers + end-to-end
// ===========================================================================

// A7. F5 SYMMETRIC-PLATE SIGNED-NORMAL TIE-BREAK. A 100×100×5 plate has a +Z top
//   face and a -Z bottom face with IDENTICAL area (10000 mm²) and mirror-symmetric
//   centroids. Their pure scores differ by only ~0.014 (well within the 0.05 ambiguous
//   band), so the main margin gate would normally dangle. The selector is built for
//   the BOTTOM face (axis -Z). The signed-normal tie-break must resolve to the bottom
//   face (negative-Z axis) and NOT to the top face (positive-Z axis). The safety gate
//   (best_align > 0.5 AND gap > 0.5) must hold: top/bottom gives aligns ~+1 vs ~-1,
//   gap ~2 >> 0.5. A genuinely-ambiguous axis-less pair (two vertices equidistant)
//   must still dangle (confirming the §5 vertex case is untouched).
TEST_CASE("A7 F5 symmetric plate: signed-normal tie-break resolves bottom face, vertex tie still dangles") {
    // Build plate-like face TopoInfos: top (+Z, index 0) and bottom (-Z, index 1).
    // Area 10000, centroids ±2.5 on Z, selector centred exactly between them (z=0).
    auto face_info = [](int idx, Eigen::Vector3d centroid, Eigen::Vector3d axis, double area) {
        TopoInfo t;
        t.kind = "face";
        t.index = idx;
        t.surf = std::string("plane");
        t.centroid = centroid;
        t.axis = axis;
        t.measure = area;
        t.owner = std::nullopt;
        t.hint_index = idx;
        return t;
    };

    // The selector represents a previously-picked "bottom" face (axis -Z, centroid z=0).
    // In a stored selector the centroid is the ACTUAL centroid of the bottom face at pick
    // time; here we place the selector centroid exactly at the bottom face's centroid so
    // the bottom face has a marginally better score than the top — mirroring the real
    // plate case where a slight centroid distance difference yields margin ~0.014.
    const double AREA = 10000.0;
    TopoInfo top    = face_info(0, {50, 50,  2.5}, { 0,  0,  1}, AREA);
    TopoInfo bottom = face_info(1, {50, 50, -2.5}, { 0,  0, -1}, AREA);

    // Selector: face, surf=plane, axis=-Z, centroid at bottom face's centroid (z=-2.5).
    // This mirrors what make_selector would emit for the bottom face at pick time.
    json sel_bottom;
    sel_bottom["kind"]          = "face";
    sel_bottom["surf"]          = "plane";
    sel_bottom["centroid"]      = json::array({50.0, 50.0, -2.5});
    sel_bottom["axis"]          = json::array({0.0, 0.0, -1.0});  // -Z = bottom normal
    sel_bottom["area_or_len"]   = AREA;
    sel_bottom["owner_feature"] = nullptr;
    sel_bottom["hint_index"]    = 1;

    // bbox_diag for a 100×100×5 plate ≈ 141.4.
    double diag = std::sqrt(100.0*100.0 + 100.0*100.0 + 5.0*5.0);

    std::vector<TopoInfo> plate_faces = {top, bottom};
    ResolveResult r = resolve(sel_bottom, plate_faces, diag);

    // Must resolve to the bottom face (index 1) not dangle.
    REQUIRE(r.index.has_value());
    CHECK(*r.index == 1);   // bottom face (-Z)
    CHECK_FALSE(r.dangling_reason.has_value());
    MESSAGE("A7 F5 plate-bottom: resolved index=" << *r.index
            << " score=" << r.score << " second=" << r.second_score);

    // Sanity: selector with axis +Z (top face) must resolve to the TOP face (index 0).
    json sel_top;
    sel_top["kind"]          = "face";
    sel_top["surf"]          = "plane";
    sel_top["centroid"]      = json::array({50.0, 50.0, 2.5});
    sel_top["axis"]          = json::array({0.0, 0.0, 1.0});  // +Z = top normal
    sel_top["area_or_len"]   = AREA;
    sel_top["owner_feature"] = nullptr;
    sel_top["hint_index"]    = 0;

    ResolveResult r2 = resolve(sel_top, plate_faces, diag);
    REQUIRE(r2.index.has_value());
    CHECK(*r2.index == 0);  // top face (+Z)
    CHECK_FALSE(r2.dangling_reason.has_value());
    MESSAGE("A7 F5 plate-top: resolved index=" << *r2.index);

    // §5 safety: two axis-LESS vertices equidistant from selector must still dangle.
    // This confirms the signed-normal fix doesn't bleed into vertex resolution.
    std::vector<TopoInfo> sym_verts = {
        vtx(10,  10, 0, 0),
        vtx(11, -10, 0, 0),
    };
    ResolveResult rv = resolve(vtx_selector(0, 0, 0), sym_verts, 100.0);
    CHECK_FALSE(rv.index.has_value());
    REQUIRE(rv.dangling_reason.has_value());
    CHECK(rv.dangling_reason->find("margin") != std::string::npos);
    MESSAGE("A7 F5 vertex-sym still dangles: " << *rv.dangling_reason);
}

// A8. AREA tie-break for COAXIAL cylinders (a bore vs the boss around it). Both share the +Z
//   axis and the SAME on-axis centroid, so their scores are identical (margin 0) and the
//   signed-axis break can't separate them (same axis) — the selector's recorded AREA does.
//   This is the FEA "load on a bored boss's inner cylinder" case that dangled before.
TEST_CASE("A8 coaxial cylinders resolve by selector area (bore vs boss)") {
    auto cyl = [](int idx, double area) {
        TopoInfo t;
        t.kind = "face";
        t.index = idx;
        t.surf = std::string("cylinder");
        t.centroid = Eigen::Vector3d(0, 0, 20);   // both coaxial, same on-axis centroid
        t.axis = Eigen::Vector3d(0, 0, 1);
        t.measure = area;
        t.hint_index = idx;
        return t;
    };
    const double PI = 3.14159265358979323846;
    const double BORE = 2.0 * PI * 12.5 * 40.0;  // ~3141 mm²
    const double BOSS = 2.0 * PI * 22.0 * 30.0;  // ~4147 mm²
    std::vector<TopoInfo> faces = {cyl(0, BORE), cyl(1, BOSS)};
    const double diag = 120.0;

    // Selector recorded for the BORE -> must resolve the bore (index 0), not dangle.
    json sel;
    sel["kind"] = "face";
    sel["surf"] = "cylinder";
    sel["centroid"] = json::array({0.0, 0.0, 20.0});
    sel["axis"] = json::array({0.0, 0.0, 1.0});
    sel["area_or_len"] = BORE;
    sel["hint_index"] = 0;
    ResolveResult rb = resolve(sel, faces, diag);
    REQUIRE(rb.index.has_value());
    CHECK(*rb.index == 0);
    MESSAGE("A8 bore resolved index=" << *rb.index);

    // Selector recorded for the BOSS -> resolves the boss (index 1).
    sel["area_or_len"] = BOSS;
    sel["hint_index"] = 1;
    ResolveResult rs = resolve(sel, faces, diag);
    REQUIRE(rs.index.has_value());
    CHECK(*rs.index == 1);

    // Two EQUAL-area coaxial cylinders minted from the SAME recorded snapshot (the
    // selector's centroid + area byte-match BOTH, since the two are coincident
    // duplicates): the exact-snapshot fast path (asm-FEA F1) resolves the deterministic
    // sort winner — for coincident duplicates either face denotes the same set of points
    // (see the fast-path comment in selectors.cpp), so resolving is correct, not a guess.
    std::vector<TopoInfo> equal = {cyl(0, BORE), cyl(1, BORE)};
    sel["area_or_len"] = BORE;
    sel["hint_index"] = 0;
    ResolveResult re = resolve(sel, equal, diag);
    REQUIRE(re.index.has_value());
    CHECK(*re.index == 0);  // deterministic: lowest hint_index wins the exact tie
    MESSAGE("A8 coincident duplicates resolved index=" << *re.index);

    // ...but a genuine area tie that is NOT a byte-exact snapshot (here the selector's
    // centroid matches NEITHER candidate) must still dangle — no guess: the signed-axis
    // break sees co-directional axes (alignment gap 0) and the coaxial area break sees
    // equal areas (gap 0), so both tie-breaks correctly refuse to pick.
    sel["centroid"] = json::array({0.0, 0.0, 0.0});
    ResolveResult rg = resolve(sel, equal, diag);
    CHECK_FALSE(rg.index.has_value());
    REQUIRE(rg.dangling_reason.has_value());
    MESSAGE("A8 equal-area non-snapshot tie still dangles: " << *rg.dangling_reason);
}

// A9. A planar face recorded before its coplanar neighbours were unified (an older engine
//   left the split; fuse_unify/cut_unify now merges it) moved centroid and grew in area, so
//   the score dangles. The plane + signed normal + "at least as large" names ONE face; the
//   6-axis arm's mates m11..m21 dangled on exactly this after the unify fix.
TEST_CASE("A9 planar selector resolves to the unique coplanar merged face") {
    auto plane = [](int idx, Eigen::Vector3d c, Eigen::Vector3d n, double area) {
        TopoInfo t;
        t.kind = "face";
        t.index = idx;
        t.surf = std::string("plane");
        t.centroid = c;
        t.axis = n;
        t.rep_dir = n;
        t.measure = area;
        t.hint_index = idx;
        return t;
    };
    const Eigen::Vector3d down(0, 0, -1), up(0, 0, 1);
    std::vector<TopoInfo> faces = {
        plane(0, {45, 0, 173}, down, 2600.0),  // merged bottom (recorded piece + neighbour)
        plane(1, {0, 0, 216}, up, 2600.0),     // top
        plane(2, {-40, 0, 173}, down, 300.0),  // smaller coplanar pad: cannot contain it
    };
    const double diag = 60.0;
    json sel;
    sel["kind"] = "face";
    sel["surf"] = "plane";
    sel["centroid"] = json::array({0.0, 0.0, 173.0});
    sel["axis"] = json::array({0.0, 0.0, 1.0});
    sel["normal"] = json::array({0.0, 0.0, -1.0});
    sel["area_or_len"] = 1262.0;
    sel["hint_index"] = 4;
    ResolveResult r = resolve(sel, faces, diag);
    REQUIRE(r.index.has_value());
    CHECK(*r.index == 0);

    // Two coplanar same-facing faces large enough to contain it: no guess.
    faces[2].measure = 2600.0;
    ResolveResult amb = resolve(sel, faces, diag);
    CHECK_FALSE(amb.index.has_value());
}

// B7. describe_faces over a 20x10x6 box (min corner at origin): 6 planar faces,
//   each with a unit axis, areas summing to the box surface area, and the +Z top
//   face is identifiable (centroid z=6, axis ~ +/-Z, area=200).
TEST_CASE("B7 describe_faces(box): 6 planes, unit axes, areas + the +Z top face") {
    const double DX = 20.0, DY = 10.0, DZ = 6.0;
    TopoDS_Shape box = geom::make_box(DX, DY, DZ);
    std::vector<TopoInfo> faces = chudware::doc::describe_faces(box);

    REQUIRE(faces.size() == 6);
    double area_sum = 0.0;
    int top_idx = -1;
    for (const TopoInfo& f : faces) {
        CHECK(f.surf.has_value());
        CHECK(*f.surf == "plane");
        REQUIRE(f.axis.has_value());
        CHECK(f.axis->norm() == doctest::Approx(1.0).epsilon(1e-9));  // unit axis
        REQUIRE(f.measure.has_value());
        area_sum += *f.measure;
        // The +Z top face: centroid z == DZ, axis ~ Z, area == DX*DY.
        if (std::abs(f.centroid.z() - DZ) < 1e-6 &&
            std::abs(std::abs(f.axis->z()) - 1.0) < 1e-6) {
            top_idx = f.index;
            CHECK(*f.measure == doctest::Approx(DX * DY));  // 200
            CHECK(f.centroid.x() == doctest::Approx(DX / 2));
            CHECK(f.centroid.y() == doctest::Approx(DY / 2));
        }
    }
    // Total surface area of the box: 2*(dx*dy + dy*dz + dz*dx).
    double expect = 2.0 * (DX * DY + DY * DZ + DZ * DX);
    CHECK(area_sum == doctest::Approx(expect));
    CHECK(top_idx >= 0);  // the +Z top face was found
    MESSAGE("B7: 6 faces, area_sum=" << area_sum << " (expect " << expect
            << "), top +Z face index=" << top_idx);
}

// B8. describe_edges -> line edges; describe_vertices -> corner vertices.
//   NOTE the explorer semantics (matched bit-for-bit from selectors.py): the
//   describers use TopExp_Explorer, which descends shape->face->wire->edge->vertex
//   and visits each sub-shape ONCE PER PARENT — it does NOT deduplicate (that would
//   be TopExp::MapShapes, as tessellate.cpp uses for stable 1-based ids). A box's 12
//   unique edges are each shared by 2 faces -> 24 edge occurrences; its 8 unique
//   vertices are each shared by edges -> 48 vertex occurrences (2 verts * 24 edges).
//   This is the FAITHFUL port behaviour; the resolution algorithm is robust to the
//   duplication (identical duplicates only ever co-rank). Every occurrence is still
//   a correctly-described line edge / corner vertex, which is what we assert here.
TEST_CASE("B8 describe_edges/vertices(box): line edges + corner vertices (explorer counts)") {
    const double DX = 20.0, DY = 10.0, DZ = 6.0;
    TopoDS_Shape box = geom::make_box(DX, DY, DZ);

    std::vector<TopoInfo> edges = chudware::doc::describe_edges(box);
    REQUIRE(edges.size() == 24);  // 12 unique edges x 2 incident faces (explorer dup)
    double len_sum = 0.0;
    for (const TopoInfo& e : edges) {
        REQUIRE(e.surf.has_value());
        CHECK(*e.surf == "line");
        REQUIRE(e.axis.has_value());           // line direction
        CHECK(e.axis->norm() == doctest::Approx(1.0).epsilon(1e-9));
        REQUIRE(e.measure.has_value());
        len_sum += *e.measure;
    }
    // Unique total length is 4*(dx+dy+dz); each unique edge appears twice -> *2.
    CHECK(len_sum == doctest::Approx(2.0 * 4.0 * (DX + DY + DZ)));

    std::vector<TopoInfo> verts = chudware::doc::describe_vertices(box);
    REQUIRE(verts.size() == 48);  // 8 unique corners, visited once per edge occurrence
    for (const TopoInfo& v : verts) {
        CHECK_FALSE(v.surf.has_value());   // vertices have no surf
        CHECK_FALSE(v.axis.has_value());
        CHECK_FALSE(v.measure.has_value());
        // Each corner coordinate is either 0 or the box extent on that axis.
        auto on = [](double c, double ext) {
            return std::abs(c) < 1e-6 || std::abs(c - ext) < 1e-6;
        };
        CHECK(on(v.centroid.x(), DX));
        CHECK(on(v.centroid.y(), DY));
        CHECK(on(v.centroid.z(), DZ));
    }
    MESSAGE("B8: " << edges.size() << " edge occurrences len_sum=" << len_sum
            << " (expect " << 2.0 * 4.0 * (DX + DY + DZ) << "), "
            << verts.size() << " vertex occurrences");
}

// B9. describe_faces over a cylinder (r=5, h=10): exactly ONE cylindrical lateral
//   face (axis ~ the cylinder axis +Z) plus 2 planar caps.
TEST_CASE("B9 describe_faces(cylinder): 1 cylinder face + 2 planar caps") {
    const double R = 5.0, Hgt = 10.0;
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(R, Hgt).Shape();  // axis = +Z by default
    std::vector<TopoInfo> faces = chudware::doc::describe_faces(cyl);

    int n_cyl = 0, n_plane = 0, n_other = 0;
    for (const TopoInfo& f : faces) {
        REQUIRE(f.surf.has_value());
        if (*f.surf == "cylinder") {
            ++n_cyl;
            REQUIRE(f.axis.has_value());
            CHECK(std::abs(std::abs(f.axis->z()) - 1.0) < 1e-6);  // axis ~ +/-Z
        } else if (*f.surf == "plane") {
            ++n_plane;
        } else {
            ++n_other;
        }
    }
    CHECK(n_cyl == 1);
    CHECK(n_plane == 2);
    CHECK(n_other == 0);
    MESSAGE("B9: cylinder faces=" << n_cyl << " planar caps=" << n_plane
            << " other=" << n_other);
}

// B9b. describe_faces over a REVOLVED stepped shaft (Ø30 x 40 then Ø20 x 60 along +X).
//   MakeRevol gives some planar faces a LEFT-handed gp_Pln frame, whose surface normal is
//   -Axis().Direction(); the old "Axis(), flipped if REVERSED" idiom read the x=0 end as
//   +X. Every planar face's signed axis/rep_dir must be its OUTWARD normal, in BOTH profile
//   windings, and each cylinder's axis_point must sit on the X axis.
TEST_CASE("B9b describe_faces(revolved shaft): outward plane normals + on-axis axis_point") {
    const double pts[6][2] = {{0, 0}, {0, 15}, {40, 15}, {40, 10}, {100, 10}, {100, 0}};
    for (int flip = 0; flip < 2; ++flip) {
        BRepBuilderAPI_MakePolygon poly;
        for (int i = 0; i < 6; ++i) {
            const int k = flip ? 5 - i : i;
            poly.Add(gp_Pnt(pts[k][0], 0.0, pts[k][1]));
        }
        poly.Close();
        const TopoDS_Face prof = BRepBuilderAPI_MakeFace(poly.Wire(), true).Face();
        const TopoDS_Shape shaft = geom::revolve_face(
            prof, Eigen::Vector3d::Zero(), Eigen::Vector3d::UnitX(), 2.0 * 3.14159265358979323846);
        REQUIRE(!shaft.IsNull());
        int n_plane = 0, n_cyl = 0;
        for (const TopoInfo& f : chudware::doc::describe_faces(shaft)) {
            REQUIRE(f.surf.has_value());
            if (*f.surf == "plane") {
                ++n_plane;
                REQUIRE(f.axis.has_value());
                REQUIRE(f.rep_dir.has_value());
                const double want = f.centroid.x() < 1e-6 ? -1.0 : 1.0;  // x=0 end faces -X
                CHECK(f.axis->x() == doctest::Approx(want));
                CHECK(f.rep_dir->x() == doctest::Approx(want));
                CHECK_FALSE(f.axis_point.has_value());
                CHECK(make_selector(f)["side"] == (want < 0 ? "left" : "right"));
            } else if (*f.surf == "cylinder") {
                ++n_cyl;
                REQUIRE(f.axis_point.has_value());
                CHECK(f.axis_point->x() == doctest::Approx(f.centroid.x()));
                CHECK(std::abs(f.axis_point->y()) < 1e-9);
                CHECK(std::abs(f.axis_point->z()) < 1e-9);
                CHECK(make_selector(f)["axis_point"].is_array());
            }
        }
        CHECK(n_plane == 3);
        CHECK(n_cyl == 2);
    }
}

// B10. END-TO-END (the point of the whole module). Describe the box faces, build a
//   selector from the +Z top face, and resolve it back -> picks that same face.
//   Then build a DIFFERENTLY-SIZED box and resolve the SAME selector -> still picks
//   the corresponding +Z top face. Resolution SURVIVES a dimension change (§5 spirit).
TEST_CASE("B10 end-to-end: selector resolves to its face, and survives a resize") {
    auto bbox_diag = [](double dx, double dy, double dz) {
        return std::sqrt(dx * dx + dy * dy + dz * dz);
    };

    // --- Original box: build a selector from its +Z top face. ---
    const double DX = 20.0, DY = 10.0, DZ = 6.0;
    TopoDS_Shape box = geom::make_box(DX, DY, DZ);
    std::vector<TopoInfo> faces = chudware::doc::describe_faces(box);

    int top_idx = -1;
    const TopoInfo* top = nullptr;
    for (const TopoInfo& f : faces) {
        if (std::abs(f.centroid.z() - DZ) < 1e-6 && f.axis.has_value() &&
            std::abs(std::abs(f.axis->z()) - 1.0) < 1e-6) {
            top_idx = f.index;
            top = &f;
        }
    }
    REQUIRE(top != nullptr);
    json sel = make_selector(*top);
    // Spot-check the emitted selector dict shape.
    CHECK(sel["kind"] == "face");
    CHECK(sel["surf"] == "plane");
    CHECK(sel["centroid"].is_array());
    CHECK(sel["centroid"].size() == 3);
    CHECK(sel["axis"].is_array());
    CHECK(sel["area_or_len"].get<double>() == doctest::Approx(DX * DY));
    // GSL Stage 0 informational fields: a box cap is a planar face -> signed normal + a
    // principal side word, no concavity/radius (it is not round). These never affect resolve().
    CHECK(sel["normal"].is_array());
    CHECK(sel["normal"].size() == 3);
    CHECK((sel["side"] == "top" || sel["side"] == "bottom"));  // axis-aligned cap
    CHECK(sel["concavity"].is_null());  // planar face has no bore/boss concavity
    CHECK(sel["radius"].is_null());     // planar face has no radius

    // Resolve the selector against the SAME box's faces -> the top face's index.
    ResolveResult r0 = resolve(sel, faces, bbox_diag(DX, DY, DZ));
    REQUIRE(r0.index.has_value());
    CHECK(*r0.index == top_idx);
    CHECK_FALSE(r0.dangling_reason.has_value());

    // --- Resize: a DIFFERENT box. The SAME selector must still find the +Z top. ---
    const double DX2 = 22.0, DY2 = 11.0, DZ2 = 6.5;
    TopoDS_Shape box2 = geom::make_box(DX2, DY2, DZ2);
    std::vector<TopoInfo> faces2 = chudware::doc::describe_faces(box2);

    ResolveResult r1 = resolve(sel, faces2, bbox_diag(DX2, DY2, DZ2));
    REQUIRE(r1.index.has_value());
    CHECK_FALSE(r1.dangling_reason.has_value());
    // The resolved face on the resized box must itself be the +Z top face.
    const TopoInfo& got = faces2[*r1.index];
    CHECK(*got.surf == "plane");
    REQUIRE(got.axis.has_value());
    CHECK(std::abs(std::abs(got.axis->z()) - 1.0) < 1e-6);   // axis ~ Z
    CHECK(got.centroid.z() == doctest::Approx(DZ2));         // it is the TOP (z=DZ2)
    MESSAGE("B10: orig top idx=" << top_idx << " resolved=" << *r0.index
            << "; resized resolved idx=" << *r1.index
            << " centroid.z=" << got.centroid.z() << " (DZ2=" << DZ2 << ")");
}

// B11. GSL Stage 0 side label: oblique faces (no dominant axis) must NOT claim a side.
TEST_CASE("B11 oblique face: side is null when rep_dir is not axis-aligned") {
    TopoInfo t;
    t.kind = "face";
    t.index = 0;
    t.surf = std::string("plane");
    t.centroid = Eigen::Vector3d(10, 10, 10);
    t.axis = Eigen::Vector3d(1, 0, 1).normalized();  // 45° in XZ — scored fingerprint only
    t.measure = 100.0;
    t.hint_index = 0;
    t.rep_dir = Eigen::Vector3d(1, 0, 1).normalized();  // oblique outward direction
    json sel = make_selector(t);
    CHECK(sel["normal"].is_array());
    CHECK(sel["side"].is_null());  // contract: null for oblique faces
}

// B12. A pick must retain every field of the explicitly enumerated descriptor, not
// merely resolve to something nearby. Include detached curved bodies so their normals
// depend on the WHOLE compound's bbox, and reverse topology to exercise signed normals
// and cylinder concavity. Sparse owners also pin the zero-based descriptor owner map.
TEST_CASE("B12 face picks exactly match explicit descriptors across analytic surfaces") {
    const gp_Ax2 cylinder_axis(gp_Pnt(40, 20, 10), gp_Dir(1, 2, 3));
    const gp_Ax2 torus_axis(gp_Pnt(5, -40, 12), gp_Dir(1, 1, 2));
    std::vector<std::pair<std::string, TopoDS_Shape>> fixtures = {
        {"plane", geom::make_box(20, 10, 6)},
        {"cylinder", BRepPrimAPI_MakeCylinder(cylinder_axis, 5, 12).Shape()},
        {"cone", BRepPrimAPI_MakeCone(7, 3, 14).Shape()},
        {"sphere", BRepPrimAPI_MakeSphere(gp_Pnt(-25, 15, 8), 6).Shape()},
        {"torus", BRepPrimAPI_MakeTorus(torus_axis, 9, 2).Shape()},
    };
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    for (const auto& fixture : fixtures) builder.Add(compound, fixture.second);
    fixtures.emplace_back("mixed", compound);

    for (const auto& fixture : fixtures) {
        for (bool reversed : {false, true}) {
            CAPTURE(fixture.first);
            CAPTURE(reversed);
            const TopoDS_Shape shape = reversed ? fixture.second.Reversed() : fixture.second;
            const auto plain = chudware::doc::describe_faces(shape);
            REQUIRE_FALSE(plain.empty());
            std::map<int, std::string> owners;
            for (std::size_t i = 0; i < plain.size(); i += 2)
                owners.emplace(static_cast<int>(i), "feature_" + std::to_string(i));
            const auto owned = chudware::doc::describe_faces(shape, owners);
            REQUIRE(owned.size() == plain.size());
            bool found_named_surface = fixture.first == "mixed";
            for (std::size_t i = 0; i < plain.size(); ++i) {
                CAPTURE(i);
                CHECK(plain[i].index == static_cast<int>(i));
                CHECK(plain[i].hint_index == static_cast<int>(i));
                if (plain[i].surf == fixture.first) found_named_surface = true;
                const int topo_id = static_cast<int>(i) + 1;
                const auto pick = chudware::doc::make_pick_selector(
                    shape, "face", topo_id, std::nullopt);
                CHECK(pick.dump() == make_selector(plain[i]).dump());
                const auto owner = owners.find(static_cast<int>(i));
                const std::optional<std::string> expected_owner =
                    owner == owners.end() ? std::nullopt : std::optional<std::string>(owner->second);
                CHECK(owned[i].owner == expected_owner);
                const auto owned_pick = chudware::doc::make_pick_selector(
                    shape, "face", topo_id, expected_owner);
                CHECK(owned_pick.dump() == make_selector(owned[i]).dump());
                auto override_info = plain[i];
                override_info.owner = "explicit_owner";
                CHECK(chudware::doc::make_pick_selector(
                          shape, "face", topo_id, std::string("explicit_owner")).dump() ==
                      make_selector(override_info).dump());

                if (fixture.first == "mixed" &&
                    (plain[i].surf == "sphere" || plain[i].surf == "torus")) {
                    // Independent whole-shape bbox oracle: a selected face's own bbox
                    // would give a different (often degenerate) representative normal.
                    Bnd_Box bounds;
                    BRepBndLib::Add(shape, bounds);
                    double x0, y0, z0, x1, y1, z1;
                    bounds.Get(x0, y0, z0, x1, y1, z1);
                    const Eigen::Vector3d center((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2);
                    TopoInfo expected = plain[i];
                    expected.rep_dir = (plain[i].centroid - center).normalized();
                    CHECK(pick["normal"] == make_selector(expected)["normal"]);
                    CHECK_FALSE(pick["normal"].is_null());
                }
            }
            CHECK(found_named_surface);
        }
    }
}

TEST_CASE("B13 reversing a face pick retains the descriptor orientation semantics") {
    const TopoDS_Shape box = geom::make_box(20, 10, 6);
    const auto forward = chudware::doc::describe_faces(box);
    const auto reverse = chudware::doc::describe_faces(box.Reversed());
    REQUIRE(forward.size() == reverse.size());
    for (std::size_t i = 0; i < forward.size(); ++i) {
        REQUIRE(forward[i].axis.has_value());
        REQUIRE(reverse[i].axis.has_value());
        CHECK((*forward[i].axis + *reverse[i].axis).norm() == doctest::Approx(0).epsilon(1e-9));
        const auto pick = chudware::doc::make_pick_selector(
            box.Reversed(), "face", static_cast<int>(i) + 1, std::nullopt);
        CHECK(pick.dump() == make_selector(reverse[i]).dump());
        CHECK(pick["normal"] == pick["axis"]);
    }

    const TopoDS_Shape cylinder = BRepPrimAPI_MakeCylinder(5, 12).Shape();
    const auto faces = chudware::doc::describe_faces(cylinder);
    bool checked_cylinder = false;
    for (const auto& face : faces) {
        if (face.surf != "cylinder") continue;
        checked_cylinder = true;
        const auto pick = chudware::doc::make_pick_selector(
            cylinder, "face", face.index + 1, std::nullopt);
        const auto reversed = chudware::doc::make_pick_selector(
            cylinder.Reversed(), "face", face.index + 1, std::nullopt);
        CHECK(pick["concavity"] == "boss");
        CHECK(reversed["concavity"] == "bore");
        CHECK(pick["axis"] == reversed["axis"]);
        CHECK(pick["normal"] == reversed["normal"]);
        CHECK(pick["axis_point"] == reversed["axis_point"]);
        CHECK(pick["area_or_len"] == reversed["area_or_len"]);
    }
    CHECK(checked_cylinder);
}

TEST_CASE("B14 invalid picks and null or empty shapes still return null") {
    const TopoDS_Shape box = geom::make_box(20, 10, 6);
    BRep_Builder builder;
    TopoDS_Compound empty;
    builder.MakeCompound(empty);
    for (const auto& kind : {"face", "edge", "vertex"}) {
        const TopAbs_ShapeEnum type = std::string(kind) == "face" ? TopAbs_FACE :
                                     std::string(kind) == "edge" ? TopAbs_EDGE : TopAbs_VERTEX;
        TopTools_IndexedMapOfShape topology;
        TopExp::MapShapes(box, type, topology);
        REQUIRE(topology.Extent() > 0);
        CHECK(chudware::doc::make_pick_selector(
                  box, kind, topology.Extent(), std::nullopt).is_object());
        for (const int bad_id : {-1, 0, topology.Extent() + 1, std::numeric_limits<int>::max()}) {
            CAPTURE(kind);
            CAPTURE(bad_id);
            CHECK(chudware::doc::make_pick_selector(
                      box, kind, bad_id, std::string("ignored")).is_null());
        }
        CHECK(chudware::doc::make_pick_selector(
                  TopoDS_Shape(), kind, 1, std::nullopt).is_null());
        CHECK(chudware::doc::make_pick_selector(empty, kind, 1, std::nullopt).is_null());
    }
    CHECK(chudware::doc::make_pick_selector(box, "unknown", 1, std::nullopt).is_null());
}
