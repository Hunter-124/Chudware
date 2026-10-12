// ACIS SAT writer suite (core/doc/acis_write.cpp) — the revision AutoCAD gets back:
//   - DXF payload encryption is the exact inverse of AutoCAD's (incl. the "^ " escape) and
//     never splits that escape across a group-3 continuation.
//   - edge parameters written at 7.0 land on their vertices (straight = distance along the
//     unit direction, ellipse = angle from the major axis): the main AutoCAD-visible risk.
//   - 4.00 / 7.00 record layouts, body records first, header body counts, sheet sidedness.
//   - spline faces carry pcurves that reference the face's surface subtype.
//   - writer -> reader round trip keeps volume/area for analytic, B-spline and rational faces.
//   - NURBS forms of planes/cylinders/lines/circles are written as analytic records.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepGProp.hxx>
#include <BRepBuilderAPI_NurbsConvert.hxx>
#include <BRepOffsetAPI_ThruSections.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakeRevol.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepPrimAPI_MakeTorus.hxx>
#include <GProp_GProps.hxx>
#include <GeomAPI_PointsToBSpline.hxx>
#include <Geom_BSplineCurve.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Elips.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include "doc/acis_read.h"
#include "doc/acis_write.h"

using namespace chudware::doc;

namespace {

std::string decrypt(const std::string& s) {
    std::string out;
    for (size_t i = 0; i < s.size(); ++i) {
        const auto c = static_cast<unsigned char>(s[i]);
        if (c == '^' && i + 1 < s.size() && s[i + 1] == ' ') {
            out += 'A';
            ++i;
        } else {
            out += c > 32 ? static_cast<char>(159 - c) : static_cast<char>(c);
        }
    }
    return out;
}

// SAT records after the 3-line header, split into tokens ('#' terminates a record).
struct Sat {
    int version = 0, n_bodies = 0;
    std::vector<std::vector<std::string>> recs;
};

Sat parse(const std::string& text) {
    Sat s;
    std::istringstream in(text);
    std::string l1, l2, l3;
    std::getline(in, l1);
    std::getline(in, l2);
    std::getline(in, l3);
    std::istringstream h(l1);
    int n_records = 0;
    h >> s.version >> n_records >> s.n_bodies;
    std::string tok;
    std::vector<std::string> cur;
    while (in >> tok) {
        if (tok == "End-of-ACIS-data") break;
        if (tok == "#") {
            s.recs.push_back(cur);
            cur.clear();
        } else {
            cur.push_back(tok);
        }
    }
    return s;
}

int ptr(const std::string& t) {
    REQUIRE(t.size() > 1);
    REQUIRE(t[0] == '$');
    return std::stoi(t.substr(1));
}

gp_Pnt xyz(const std::vector<std::string>& r, size_t i) {
    return gp_Pnt(std::stod(r.at(i)), std::stod(r.at(i + 1)), std::stod(r.at(i + 2)));
}

TopoDS_Shape box_with_hole() {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(gp_Pnt(-5, -5, -5), 10, 10, 10).Shape();
    TopoDS_Shape cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0.3, 0.2, -10), gp_Dir(0, 0, 1)), 2.5, 20).Shape();
    return BRepAlgoAPI_Cut(box, cyl).Shape();
}

TopoDS_Shape loft() {
    BRepOffsetAPI_ThruSections l(Standard_True, Standard_False);
    l.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 5))).Wire());
    l.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Elips(gp_Ax2(gp_Pnt(1, 0, 10), gp_Dir(0, 0.2, 1)), 6, 3))).Wire());
    l.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(0, 1, 20), gp_Dir(0, 0, 1)), 2))).Wire());
    l.Build();
    return l.Shape();
}

// Surface of revolution of a B-spline profile: written as a rational spline surface whose
// pcurves are rebuilt in the converted parameters.
TopoDS_Shape revolved() {
    TColgp_Array1OfPnt pts(1, 5);
    pts(1) = gp_Pnt(2, 0, 0);
    pts(2) = gp_Pnt(3, 0, 2);
    pts(3) = gp_Pnt(2.5, 0, 4);
    pts(4) = gp_Pnt(4, 0, 6);
    pts(5) = gp_Pnt(3, 0, 8);
    Handle(Geom_BSplineCurve) prof = GeomAPI_PointsToBSpline(pts).Curve();
    TopoDS_Wire w = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(2, 0, 0)),
                                            BRepBuilderAPI_MakeEdge(prof),
                                            BRepBuilderAPI_MakeEdge(gp_Pnt(3, 0, 8), gp_Pnt(0, 0, 8)),
                                            BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 8), gp_Pnt(0, 0, 0)))
                        .Wire();
    TopoDS_Face f = BRepBuilderAPI_MakeFace(w, Standard_True).Face();
    return BRepPrimAPI_MakeRevol(f, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))).Shape();
}

TopoDS_Shape filleted_box() {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(10, 8, 6).Shape();
    BRepFilletAPI_MakeFillet f(box);
    TopTools_IndexedMapOfShape edges;
    TopExp::MapShapes(box, TopAbs_EDGE, edges);
    for (int i = 1; i <= edges.Extent(); ++i) f.Add(1.5, TopoDS::Edge(edges(i)));
    return f.Shape();
}

TopoDS_Shape triangle_sheet() {
    TopoDS_Wire w = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(4, 0, 0)),
                                            BRepBuilderAPI_MakeEdge(gp_Pnt(4, 0, 0), gp_Pnt(4, 3, 1)),
                                            BRepBuilderAPI_MakeEdge(gp_Pnt(4, 3, 1), gp_Pnt(0, 0, 0)))
                        .Wire();
    return BRepBuilderAPI_MakeFace(w, Standard_True).Face();
}

double measure(const TopoDS_Shape& s, bool solid) {
    GProp_GProps g;
    // Adaptive integration: the fixed Gauss rule is too coarse for rational faces bounded by
    // many-segment pcurves.
    if (solid) BRepGProp::VolumeProperties(s, g, 1e-10);
    else BRepGProp::SurfaceProperties(s, g, 1e-10);
    return g.Mass();
}

}  // namespace

TEST_CASE("encrypt_dxf_sat is the inverse of AutoCAD's SAT encryption") {
    const std::string sat = "400 3 1 0 \n12 ACIS 4.00 NT\nbody $-1 $1 $-1 $-1 #\nEnd-of-ACIS-data\n";
    const auto groups = encrypt_dxf_sat(sat);
    REQUIRE(groups.size() == 4);
    std::string back;
    for (const auto& [code, value] : groups) {
        CHECK(code == 1);
        back += decrypt(value) + "\n";
    }
    CHECK(back == sat);
    // 'A' (65) encrypts to '^' (94), which DXF escapes as "^ "; '#' -> '|'; spaces stay.
    CHECK(groups[1].second.substr(0, 5) == "nm ^ ");
    CHECK(groups[2].second.back() == '|');
}

TEST_CASE("encrypt_dxf_sat continues long lines in group 3 without splitting the ^ escape") {
    const std::string line = std::string(254, 'x') + "A" + std::string(10, 'y');
    const auto groups = encrypt_dxf_sat(line + "\n");
    REQUIRE(groups.size() == 2);
    CHECK(groups[0].first == 1);
    CHECK(groups[1].first == 3);
    CHECK(groups[0].second.size() == 254);  // the '^' moved to the continuation
    CHECK(groups[1].second.rfind("^ ", 0) == 0);
    CHECK(decrypt(groups[0].second + groups[1].second) == line);
    for (const auto& g : encrypt_dxf_sat(std::string(700, 'z'))) CHECK(g.second.size() <= 255);
}

TEST_CASE("encrypt_dxf_sat refuses bytes it cannot encrypt, by name") {
    try {
        encrypt_dxf_sat("body\tx #\n");
        FAIL("tab accepted");
    } catch (const std::runtime_error& e) {
        CHECK(std::string(e.what()).find("0x09") != std::string::npos);
    }
}

TEST_CASE("write_sat refuses bad options and empty input by name") {
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(1, 1, 1).Shape();
    CHECK_THROWS_WITH_AS(write_sat(box, {500, 1.0}), doctest::Contains("unsupported SAT version 500"), std::runtime_error);
    CHECK_THROWS_WITH_AS(write_sat(box, {700, 0.0}), doctest::Contains("mm_per_unit"), std::runtime_error);
    CHECK_THROWS_WITH_AS(write_sat_bodies({TopoDS_Shape()}, {700, 1.0}), doctest::Contains("no non-null shape"),
                         std::runtime_error);
    CHECK_THROWS_WITH_AS(write_sat(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(1, 0, 0)).Edge(), {700, 1.0}),
                         doctest::Contains("free edges"), std::runtime_error);
}

TEST_CASE("write_sat_bodies writes one body per non-null shape, bodies first") {
    const std::string sat = write_sat_bodies(
        {BRepPrimAPI_MakeBox(1, 2, 3).Shape(), TopoDS_Shape(), BRepPrimAPI_MakeSphere(2).Shape()}, {700, 25.4});
    const Sat s = parse(sat);
    CHECK(s.version == 700);
    CHECK(s.n_bodies == 2);
    REQUIRE(s.recs.size() > 2);
    CHECK(s.recs[0][0] == "body");
    CHECK(s.recs[1][0] == "body");
    CHECK(s.recs[2][0] != "body");
    // Header: strings length-prefixed without '@' (as ACIS writes them), units line.
    std::istringstream in(sat);
    std::string l1, l2, l3;
    std::getline(in, l1);
    std::getline(in, l2);
    std::getline(in, l3);
    CHECK(l2.find('@') == std::string::npos);
    CHECK(l2.find("ACIS 7.00 NT") != std::string::npos);
    CHECK(l3.rfind("25.4 ", 0) == 0);
    CHECK(sat.find("End-of-ACIS-data") != std::string::npos);
    // The bodies read back as two separate solids.
    const AcisReadResult r = read_acis(sat);
    REQUIRE(r.bodies.size() == 2);
    CHECK(r.mm_per_unit == doctest::Approx(25.4));
    CHECK(measure(r.bodies[0].shape, true) == doctest::Approx(6.0).epsilon(1e-9));
}

TEST_CASE("7.0 edge parameters land on their vertices (lines and circles)") {
    const Sat s = parse(write_sat(box_with_hole(), {700, 1.0}));
    int lines = 0, circles = 0;
    for (const auto& r : s.recs) {
        if (r[0] != "edge") continue;
        // edge $attr -1 $pattern $v0 t0 $v1 t1 $coedge $curve sense @7 unknown
        REQUIRE(r.size() == 13);
        CHECK(r[10] == "forward");
        const double t[2] = {std::stod(r[5]), std::stod(r[7])};
        const int v[2] = {ptr(r[4]), ptr(r[6])};
        const auto& c = s.recs.at(ptr(r[9]));
        for (int k = 0; k < 2; ++k) {
            const auto& vert = s.recs.at(v[k]);
            const gp_Pnt P = xyz(s.recs.at(ptr(vert[5])), 4);
            gp_Pnt Q;
            if (c[0] == "straight-curve") {
                Q = xyz(c, 4).Translated(gp_Vec(xyz(c, 7).XYZ()) * t[k]);
            } else {
                REQUIRE(c[0] == "ellipse-curve");
                const gp_Vec n(xyz(c, 7).XYZ()), maj(xyz(c, 10).XYZ());
                const double ratio = std::stod(c[13]);
                Q = xyz(c, 4).Translated(maj * std::cos(t[k]) + n.Crossed(maj) * (ratio * std::sin(t[k])));
            }
            CHECK(Q.Distance(P) < 1e-9);
        }
        (c[0] == "straight-curve" ? lines : circles)++;
    }
    CHECK(lines == 12);  // the box edges; the hole has no seam edge (ACIS band face)
    CHECK(circles == 2);
}

TEST_CASE("4.00 and 7.00 record layouts") {
    const TopoDS_Shape shape = box_with_hole();
    const Sat v4 = parse(write_sat(shape, {400, 1.0}));
    const Sat v7 = parse(write_sat(shape, {700, 1.0}));
    REQUIRE(v4.recs.size() == v7.recs.size());
    for (size_t i = 0; i < v4.recs.size(); ++i) {
        CHECK(v4.recs[i][0] == v7.recs[i][0]);
        // 7.0 adds the entity id and pattern pointer after the attribute pointer.
        CHECK(v7.recs[i][2] == "-1");
        CHECK(v7.recs[i][3] == "$-1");
        if (v4.recs[i][0] == "edge") {
            CHECK(v4.recs[i].size() == 7);   // edge $attr $v0 $v1 $coedge $curve sense
            CHECK(v7.recs[i].size() == 13);  // + id, pattern, t0, t1, @7 unknown
        } else {
            CHECK(v7.recs[i].size() == v4.recs[i].size() + 2);
        }
    }
}

TEST_CASE("solid faces are single-sided, sheet faces double-sided") {
    for (const auto& r : parse(write_sat(box_with_hole(), {400, 1.0})).recs)
        if (r[0] == "face") CHECK(r.back() == "single");
    for (const auto& r : parse(write_sat(triangle_sheet(), {400, 1.0})).recs)
        if (r[0] == "face") {
            CHECK(r[r.size() - 2] == "double");
            CHECK(r.back() == "out");
        }
}

TEST_CASE("coedges on spline faces carry pcurves referencing the face's surface") {
    for (int version : {400, 700}) {
        const std::string sat = write_sat(loft(), {version, 1.0});
        const Sat s = parse(sat);
        const size_t o = version >= 700 ? 2 : 0;
        int spline_coedges = 0;
        for (const auto& r : s.recs) {
            if (r[0] != "coedge") continue;
            const auto& loop = s.recs.at(ptr(r[7 + o]));
            const auto& face = s.recs.at(ptr(loop[4 + o]));
            const bool on_spline = s.recs.at(ptr(face[6 + o]))[0] == "spline-surface";
            CHECK((ptr(r[8 + o]) >= 0) == on_spline);
            if (on_spline) {
                ++spline_coedges;
                CHECK(s.recs.at(ptr(r[8 + o]))[0] == "pcurve");
            }
        }
        CHECK(spline_coedges > 0);
        // Every `{ ref n }` names an earlier exactsur definition (subtypes counted in file order).
        std::vector<std::string> toks;
        std::istringstream in(sat);
        for (std::string tok; in >> tok;) toks.push_back(tok);
        std::vector<std::string> defs;
        int refs = 0;
        for (size_t i = 0; i + 2 < toks.size(); ++i) {
            if (toks[i] != "{") continue;
            if (toks[i + 1] != "ref") {
                defs.push_back(toks[i + 1]);
                continue;
            }
            const int n = std::stoi(toks[i + 2]);
            REQUIRE(n < static_cast<int>(defs.size()));
            CHECK(defs[n] == "exactsur");
            ++refs;
        }
        CHECK(refs == spline_coedges);  // one reference per pcurve
    }
}

TEST_CASE("NURBS forms of analytic geometry are written as analytic records") {
    // A box and a box with a cylindrical hole converted to B-splines (what imports and some
    // modelling ops hand us) must come out as AutoCAD writes them: planes, cone (cylinder),
    // straight and ellipse curves, no spline/intcurve records.
    struct Case {
        TopoDS_Shape shape;
        int planes, cones, straights, ellipses;
    };
    const std::vector<Case> cases = {
        {BRepBuilderAPI_NurbsConvert(BRepPrimAPI_MakeBox(gp_Pnt(-1, -2, -3), 10, 20, 30).Shape()).Shape(), 6, 0, 12, 0},
        {BRepBuilderAPI_NurbsConvert(box_with_hole()).Shape(), 6, 1, 12, 2},
    };
    for (const auto& c : cases) {
        for (int version : {400, 700}) {
            CAPTURE(version);
            const std::string sat = write_sat(c.shape, {version, 1.0});
            std::map<std::string, int> n;
            for (const auto& r : parse(sat).recs) ++n[r[0]];
            CHECK(n["plane-surface"] == c.planes);
            CHECK(n["cone-surface"] == c.cones);
            CHECK(n["straight-curve"] == c.straights);
            CHECK(n["ellipse-curve"] == c.ellipses);
            CHECK(n["spline-surface"] == 0);
            CHECK(n["intcurve-curve"] == 0);
            CHECK(n["pcurve"] == 0);
            const AcisReadResult r = read_acis(sat);
            REQUIRE(r.bodies.size() == 1);
            CHECK(r.bodies[0].solid);
            CHECK(measure(r.bodies[0].shape, true) == doctest::Approx(measure(c.shape, true)).epsilon(1e-6));
        }
    }
}

TEST_CASE("writer -> reader round trip keeps volume and area") {
    struct Case {
        const char* name;
        TopoDS_Shape shape;
        bool solid;
    };
    const std::vector<Case> cases = {
        {"box_hole", box_with_hole(), true},
        {"sphere", BRepPrimAPI_MakeSphere(gp_Pnt(1, 2, 3), 4).Shape(), true},
        {"torus", BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 1), gp_Dir(0, 1, 1)), 10, 2).Shape(), true},
        {"fillet", filleted_box(), true},
        {"loft", loft(), true},
        {"revolved", revolved(), true},
        {"sheet", triangle_sheet(), false},
    };
    for (const auto& c : cases) {
        for (int version : {400, 700}) {
            CAPTURE(c.name);
            CAPTURE(version);
            const AcisReadResult r = read_acis(write_sat(c.shape, {version, 1.0}));
            REQUIRE(r.bodies.size() == 1);
            CHECK(r.warnings.empty());
            CHECK(r.bodies[0].solid == c.solid);
            CHECK(measure(r.bodies[0].shape, c.solid) ==
                  doctest::Approx(measure(c.shape, c.solid)).epsilon(1e-6));
        }
    }
}
