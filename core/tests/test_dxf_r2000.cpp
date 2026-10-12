// DXF writer version selection and the R2000 (AC1015) structure that carries ACIS bodies:
// documents without ACIS stay R12; with ACIS every handle is unique and below $HANDSEED,
// every owner exists, SAT survives the 1/3 group encryption (incl. >255-char lines and
// 'A' escapes), and unsupported inputs are refused by name.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "doc/dxf.h"

using namespace chudware::doc;

namespace {

using Pairs = std::vector<std::pair<int, std::string>>;

Pairs parse(const std::string& dxf) {
    Pairs out;
    std::size_t pos = 0;
    auto line = [&]() {
        const std::size_t e = dxf.find("\r\n", pos);
        REQUIRE(e != std::string::npos);
        std::string s = dxf.substr(pos, e - pos);
        pos = e + 2;
        return s;
    };
    while (pos < dxf.size()) {
        const int code = std::stoi(line());
        out.emplace_back(code, line());
    }
    return out;
}

// Independent oracle for AutoCAD's SAT "encryption": 33..126 -> 159-c, "^ " -> 'A'.
std::string decrypt(const std::string& s) {
    std::string out;
    for (std::size_t i = 0; i < s.size(); ++i) {
        const unsigned char c = static_cast<unsigned char>(s[i]);
        if (c == '^') {
            out.push_back('A');
            ++i;  // the escape's trailing space
        } else if (c >= 33 && c <= 126) {
            out.push_back(static_cast<char>(159 - c));
        } else {
            out.push_back(s[i]);
        }
    }
    return out;
}

// SAT lines of the n-th entity of `type` (1 starts a line, 3 continues it).
std::vector<std::string> sat_lines(const Pairs& p, const std::string& type, int n = 0) {
    std::vector<std::string> lines;
    for (std::size_t i = 0; i < p.size(); ++i) {
        if (p[i].first != 0 || p[i].second != type || n-- != 0) continue;
        for (std::size_t j = i + 1; j < p.size() && p[j].first != 0; ++j) {
            if (p[j].first == 1) lines.push_back(p[j].second);
            else if (p[j].first == 3) lines.back() += p[j].second;
        }
        break;
    }
    for (auto& l : lines) l = decrypt(l);
    return lines;
}

std::string header_value(const Pairs& p, const std::string& var) {
    for (std::size_t i = 0; i + 1 < p.size(); ++i)
        if (p[i].first == 9 && p[i].second == var) return p[i + 1].second;
    return {};
}

const std::string kLong = "point $-1 1." + std::string(280, '0') + " 2 3 #";

std::string sample_sat(int version = 400) {
    return std::to_string(version) +
           " 1 1 0 \n"
           "24 Chudware ACIS Writer 12 ACIS 4.00 NT 24 Sun Oct 11 20:48:03 2026 \n"
           "25.399999999999999 9.9999999999999995e-007 1e-010 \n"
           "body $-1 $-1 $-1 $1 #\n"
           "transform $-1 1 0 0 0 1 0 0 0 1 10 20 30 1 no_rotate no_reflect no_shear #\n" +
           kLong + "\nEnd-of-ACIS-data \n";
}

DxfDocument doc_2d() {
    DxfDocument d;
    d.add_layer("OUTLINE", 3, "DASHED");
    d.polylines.push_back({"OUTLINE", {{0, 0}, {10, 0}, {10, 5}}, 0.5, ""});
    d.arcs.push_back({"OUTLINE", {5, 5}, 2.0, 0, 90, false, "CENTER", 0});
    d.texts.push_back({"OUTLINE", {1, 1}, 2.5, "Ø5 ±0.1", 1, 2});
    d.faces3.push_back({"OUTLINE", {{{0, 0, 0}, {1, 0, 0}, {0, 1, 1}}}, 0x4});
    return d;
}

// Group values of the first `type` entity (first occurrence of each code).
std::map<int, std::string> entity_tags(const Pairs& p, const std::string& type) {
    std::map<int, std::string> tags;
    for (std::size_t i = 0; i < p.size(); ++i) {
        if (p[i] != std::pair<int, std::string>{0, type}) continue;
        for (std::size_t j = i + 1; j < p.size() && p[j].first != 0; ++j)
            tags.emplace(p[j].first, p[j].second);
        break;
    }
    return tags;
}

// Header point $var as {x, y, z} (z = 0 when the variable has only 10/20).
std::vector<double> header_point(const Pairs& p, const std::string& var) {
    std::vector<double> v;
    for (std::size_t i = 0; i < p.size(); ++i)
        if (p[i] == std::pair<int, std::string>{9, var})
            for (std::size_t j = i + 1; j < p.size() && p[j].first != 9 && p[j].first != 0; ++j)
                v.push_back(std::stod(p[j].second));
    return v;
}

// A circle of radius 2 in the WCS plane y = 4, centred on (10, 4, 7): extrusion +Y, whose
// Arbitrary Axis frame is X = (-1,0,0), Y = (0,0,1), so its OCS centre is (-10, 7, 4).
DxfArc tilted_circle() {
    DxfArc a;
    a.layer = "AXES";
    a.center = {-10.0, 7.0};
    a.z = 4.0;
    a.radius = 2.0;
    a.circle = true;
    a.normal = {0.0, 3.0, 0.0};  // any positive length; written as the unit vector
    return a;
}

}  // namespace

TEST_CASE("DXF without ACIS stays R12; with ACIS becomes R2000") {
    DxfDocument d = doc_2d();
    int n = 0;
    const Pairs r12 = parse(build_dxf(d, &n));
    CHECK(n == 4);
    CHECK(header_value(r12, "$ACADVER") == "AC1009");
    for (const auto& [code, v] : r12) CHECK(code != 100);  // no subclass markers in R12

    d.acis.push_back({"BODIES", sample_sat(), true, 400});
    const Pairs r2k = parse(build_dxf(d, &n));
    CHECK(n == 5);
    CHECK(header_value(r2k, "$ACADVER") == "AC1015");
}

TEST_CASE("DXF R2000 handles are unique, owned and below $HANDSEED") {
    DxfDocument d = doc_2d();
    d.insunits = 1;
    d.acis.push_back({"BODIES", sample_sat(), true, 400});
    d.acis.push_back({"", sample_sat(), false, 400});  // unnamed layer -> "0"
    const Pairs p = parse(build_dxf(d, nullptr));

    CHECK(header_value(p, "$INSUNITS") == "1");
    const unsigned long seed = std::stoul(header_value(p, "$HANDSEED"), nullptr, 16);

    std::set<unsigned long> handles;
    for (std::size_t i = 1; i < p.size(); ++i) {
        const auto& [code, v] = p[i];
        if ((code != 5 && code != 105) || p[i - 1].second == "$HANDSEED") continue;
        const unsigned long h = std::stoul(v, nullptr, 16);
        CHECK_MESSAGE(handles.insert(h).second, "duplicate handle " << v);
        CHECK(h < seed);
    }
    for (const auto& [code, v] : p)
        if (code == 330 || code == 340 || code == 350 || code == 390) {
            const unsigned long h = std::stoul(v, nullptr, 16);
            CHECK_MESSAGE((h == 0 || handles.count(h)), "dangling pointer " << code << " " << v);
        }

    // The layer only an entity references still gets a LAYER record; "0" is not duplicated.
    std::map<std::string, int> layers;
    for (std::size_t i = 0; i + 5 < p.size(); ++i)
        if (p[i] == std::pair<int, std::string>{0, "LAYER"})
            for (std::size_t j = i + 1; j < p.size() && p[j].first != 0; ++j)
                if (p[j].first == 2) ++layers[p[j].second];
    CHECK(layers == std::map<std::string, int>{{"0", 1}, {"OUTLINE", 1}, {"BODIES", 1}});

    // The solid's point (1,2,3) moved by its body transform (10,20,30) bounds X and Z.
    bool ext_ok = false;
    for (std::size_t i = 0; i + 3 < p.size(); ++i)
        if (p[i] == std::pair<int, std::string>{9, "$EXTMAX"})
            ext_ok = std::stod(p[i + 1].second) == 11.0 && std::stod(p[i + 3].second) == 33.0;
    CHECK(ext_ok);
}

TEST_CASE("DXF R2000 SAT round-trips through 1/3 groups, long lines included") {
    DxfDocument d;
    const std::string sat = sample_sat();
    d.acis.push_back({"0", sat, true, 400});
    d.acis.push_back({"0", sat, false, 400});
    const Pairs p = parse(build_dxf(d, nullptr));
    for (const auto& [code, v] : p)
        if (code == 1 || code == 3) CHECK(v.size() <= 255);
    for (const char* type : {"3DSOLID", "BODY"}) {
        std::string back;
        for (const auto& l : sat_lines(p, type)) back += l + "\n";
        CHECK_MESSAGE(back == sat, type);
    }
}

TEST_CASE("DXF R2000 refuses what it cannot write, by name") {
    DxfDocument d = doc_2d();
    d.acis.push_back({"BODIES", sample_sat(700), true, 700});
    CHECK_THROWS_WITH_AS(build_dxf(d, nullptr), doctest::Contains("SAT version 700"),
                         std::runtime_error);

    d.acis.back() = {"BODIES", sample_sat(), true, 700};
    CHECK_THROWS_WITH_AS(build_dxf(d, nullptr), doctest::Contains("declares ACIS version 700"),
                         std::runtime_error);

    d.acis.back() = {"BODIES", sample_sat(), true, 400};
    d.polylines[0].ltype = "PHANTOM";
    CHECK_THROWS_WITH_AS(build_dxf(d, nullptr), doctest::Contains("'PHANTOM'"),
                         std::runtime_error);

    d.polylines[0].ltype.clear();
    d.insunits = 42;
    CHECK_THROWS_WITH_AS(build_dxf(d, nullptr), doctest::Contains("$INSUNITS 42"),
                         std::runtime_error);
}

TEST_CASE("DXF arcs keep their OCS extrusion; +Z arcs write none") {
    for (const auto& [code, v] : parse(build_dxf(doc_2d(), nullptr))) CHECK(code != 210);

    DxfDocument d;
    d.arcs.push_back(tilted_circle());
    const Pairs r12 = parse(build_dxf(d, nullptr));
    auto c = entity_tags(r12, "CIRCLE");
    CHECK(std::stod(c[10]) == -10.0);
    CHECK(std::stod(c[20]) == 7.0);
    CHECK(std::stod(c[30]) == 4.0);
    CHECK(std::stod(c[210]) == 0.0);
    CHECK(std::stod(c[220]) == 1.0);
    CHECK(std::stod(c[230]) == 0.0);
    // Extents are the WCS circle: x 8..12 in the plane y = 4.
    CHECK(header_point(r12, "$EXTMIN") == std::vector<double>{8.0, 4.0, 0.0});
    CHECK(header_point(r12, "$EXTMAX") == std::vector<double>{12.0, 4.0, 0.0});

    d.acis.push_back({"0", sample_sat(), true, 400});  // point (11, 22, 33) after transform
    const Pairs r2k = parse(build_dxf(d, nullptr));
    c = entity_tags(r2k, "CIRCLE");
    CHECK(std::stod(c[10]) == -10.0);
    CHECK(std::stod(c[220]) == 1.0);
    CHECK(std::stod(c[210]) == 0.0);
    CHECK(std::stod(c[230]) == 0.0);
    CHECK(header_point(r2k, "$EXTMIN") == std::vector<double>{8.0, 4.0, 5.0});
    CHECK(header_point(r2k, "$EXTMAX") == std::vector<double>{12.0, 22.0, 33.0});

    d.arcs[0].normal = {0.0, 0.0, 0.0};
    CHECK_THROWS_WITH_AS(build_dxf(d, nullptr), doctest::Contains("invalid extrusion"),
                         std::runtime_error);
}
