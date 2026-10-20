// Native R2000 DWG writer (doc/dwg_write.h): the file is structurally sound on its own
// (section locators, CRCs, an object map that addresses every object by its own handle,
// map pages within their size limit), input R2000 cannot carry is refused by name, and —
// when LibreDWG's reader is installed — entities, layers and SAT text read back.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cctype>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "doc/dwg.h"
#include "doc/dwg_write.h"

namespace fs = std::filesystem;
using namespace chudware::doc;

namespace {

std::uint16_t crc16(const std::uint8_t* p, std::size_t n, std::uint16_t crc = 0xC0C1) {
    for (std::size_t i = 0; i < n; ++i) {
        crc ^= p[i];
        for (int k = 0; k < 8; ++k) crc = (crc & 1) ? (crc >> 1) ^ 0xA001 : crc >> 1;
    }
    return crc;
}

std::uint32_t rl(const std::string& b, std::size_t at) {
    std::uint32_t v = 0;
    for (int i = 3; i >= 0; --i) v = (v << 8) | static_cast<std::uint8_t>(b[at + i]);
    return v;
}
std::uint16_t rs(const std::string& b, std::size_t at) {
    return static_cast<std::uint16_t>(static_cast<std::uint8_t>(b[at]) |
                                      (static_cast<std::uint8_t>(b[at + 1]) << 8));
}

// MSB-first reader for the few bit codes the checks need.
struct BitReader {
    const std::uint8_t* p;
    std::size_t bit = 0;
    unsigned get() { const unsigned v = (p[bit >> 3] >> (7 - (bit & 7))) & 1u; ++bit; return v; }
    std::uint32_t bits(int n) { std::uint32_t v = 0; while (n--) v = (v << 1) | get(); return v; }
    std::uint8_t rc() { return static_cast<std::uint8_t>(bits(8)); }
    std::uint16_t bs() {
        switch (bits(2)) {
            case 0: { const std::uint16_t lo = rc(); return static_cast<std::uint16_t>(lo | (rc() << 8)); }
            case 1: return rc();
            case 2: return 0;
            default: return 256;
        }
    }
    std::uint32_t rl() { std::uint32_t v = 0; for (int i = 0; i < 4; ++i) v |= std::uint32_t(rc()) << (8 * i); return v; }
    std::uint64_t h() {
        const std::uint8_t cc = rc();
        std::uint64_t v = 0;
        for (int i = 0; i < (cc & 0xF); ++i) v = (v << 8) | rc();
        return v;
    }
};

struct Parsed {
    std::map<std::uint64_t, std::uint16_t> types;  // handle -> object type
    std::size_t map_pages = 0;
};

// Walks the whole file: header CRC, section locators, framed sections, every object map
// page and every object it addresses. CHECKs fail on any inconsistency.
Parsed parse(const std::string& f) {
    Parsed out;
    REQUIRE(f.size() > 0x61);
    CHECK(f.compare(0, 6, "AC1015") == 0);
    REQUIRE(rl(f, 0x15) == 6);
    const auto* u = reinterpret_cast<const std::uint8_t*>(f.data());
    CHECK(rs(f, 0x15 + 4 + 6 * 9) == crc16(u, 0x15 + 4 + 6 * 9));
    std::uint32_t addr[6], size[6];
    for (int i = 0; i < 6; ++i) {
        const std::size_t at = 0x19 + 9 * i;
        CHECK(static_cast<std::uint8_t>(f[at]) == i);
        addr[i] = rl(f, at + 1);
        size[i] = rl(f, at + 5);
        CHECK(std::uint64_t(addr[i]) + size[i] <= f.size());
    }
    // Header variables and classes: sentinel | RL size | data | CRC(size+data) | sentinel.
    for (int s : {0, 1}) {
        const std::uint32_t n = rl(f, addr[s] + 16);
        CHECK(n + 16 + 4 + 2 + 16 == size[s]);
        CHECK(rs(f, addr[s] + 20 + n) == crc16(u + addr[s] + 16, 4 + n));
    }
    // Object map pages, then each object: MS size | data | CRC, and its own handle.
    std::size_t at = addr[2];
    std::uint64_t prev_handle = 0;
    for (;;) {
        const std::size_t page_size = (static_cast<std::uint8_t>(f[at]) << 8) | static_cast<std::uint8_t>(f[at + 1]);
        CHECK(page_size <= 2032);
        const std::uint16_t crc = static_cast<std::uint16_t>((static_cast<std::uint8_t>(f[at + page_size]) << 8) |
                                                             static_cast<std::uint8_t>(f[at + page_size + 1]));
        CHECK(crc == crc16(u + at, page_size));
        if (page_size == 2) break;
        ++out.map_pages;
        std::size_t p = at + 2;
        std::uint64_t handle = 0;
        std::int64_t loc = 0;
        while (p < at + page_size) {
            std::uint64_t dh = 0;
            for (int shift = 0;; shift += 7) {
                const std::uint8_t b = u[p++];
                dh |= std::uint64_t(b & 0x7F) << shift;
                if (!(b & 0x80)) break;
            }
            std::int64_t dl = 0;
            for (int shift = 0;; shift += 7) {
                const std::uint8_t b = u[p++];
                if (b & 0x80) { dl |= std::int64_t(b & 0x7F) << shift; continue; }
                dl |= std::int64_t(b & 0x3F) << shift;
                if (b & 0x40) dl = -dl;
                break;
            }
            handle += dh;
            loc += dl;
            CHECK(handle > prev_handle);
            prev_handle = handle;
            // Object: MS size, then the bit stream starting with its type and own handle.
            std::size_t q = static_cast<std::size_t>(loc);
            std::uint32_t obj_size = 0;
            for (int shift = 0;; shift += 15) {
                const std::uint16_t w = rs(f, q);
                q += 2;
                obj_size |= std::uint32_t(w & 0x7FFF) << shift;
                if (!(w & 0x8000)) break;
            }
            REQUIRE(q + obj_size + 2 <= f.size());
            CHECK(rs(f, q + obj_size) == crc16(u + loc, q - loc + obj_size));
            BitReader r{u + q};
            const std::uint16_t type = r.bs();
            const std::uint32_t bitsize = r.rl();
            CHECK(bitsize <= obj_size * 8);
            CHECK(r.h() == handle);
            out.types[handle] = type;
        }
        at += page_size + 2;
    }
    return out;
}

DxfDocument drawing() {
    DxfDocument d;
    d.add_layer("VISIBLE", 7, "CONTINUOUS");
    d.add_layer("HIDDEN", 8, "DASHED");
    d.add_layer("CENTER", 1, "CENTER");
    d.polylines.push_back({"VISIBLE", {{0, 0}, {10, 0}, {10, 5}, {0, 5}, {0, 0}}, 0.5, ""});
    d.polylines.push_back({"HIDDEN", {{0, 1}, {10, 1}}, 0.0, ""});
    d.arcs.push_back({"CENTER", {5, 5}, 2.0, 0, 90, false, "", 0});
    d.arcs.push_back({"VISIBLE", {5, 5}, 1.0, 0, 360, true, "DASHED", 0});
    d.texts.push_back({"VISIBLE", {5.0, 2.0}, 3.5, "SCALE 1:1", 1, 2});
    d.solids.push_back({"VISIBLE", {{{0, 0}, {1, 0.3}, {1, -0.3}}}});
    d.lines3.push_back({"VISIBLE", {0, 0, 0}, {1, 2, 3}});
    d.faces3.push_back({"VISIBLE", {{{0, 0, 0}, {1, 0, 0}, {0, 1, 1}}}, 2});
    return d;
}

// A minimal ACIS 4.00 SAT text (header, an empty body, terminator); the writer carries it
// verbatim, so it need not describe geometry.
const char* kSat =
    "400 0 1 0 \n"
    "8 Chudware 12 ACIS 4.00 NT 24 Sun Oct 11 12:00:00 2026 \n"
    "1 9.9999999999999995e-007 1e-010 \n"
    "body $-1 $1 $-1 $-1 #\n"
    "End-of-ACIS-data \n";

bool upper_case_equal(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    for (std::size_t i = 0; i < a.size(); ++i)
        if (std::toupper(static_cast<unsigned char>(a[i])) != std::toupper(static_cast<unsigned char>(b[i])))
            return false;
    return true;
}

}  // namespace

TEST_CASE("Native R2000 DWG: sections, CRCs and the object map are consistent") {
    const DwgImage img = build_dwg(drawing());
    CHECK(img.entities == 8);
    const Parsed p = parse(img.bytes);
    std::map<std::uint16_t, int> count;
    for (const auto& [h, t] : p.types) ++count[t];
    // Entities by fixed type: LWPOLYLINE 77, ARC 17, CIRCLE 18, TEXT 1, SOLID 31, LINE 19, 3DFACE 28.
    CHECK(count[77] == 2);
    CHECK(count[17] == 1);
    CHECK(count[18] == 1);
    CHECK(count[1] == 1);
    CHECK(count[31] == 1);
    CHECK(count[19] == 1);
    CHECK(count[28] == 1);
    // Layers 0 + 3, linetypes ByBlock/ByLayer/Continuous/DASHED/CENTER, both block records.
    CHECK(count[51] == 4);
    CHECK(count[57] == 5);
    CHECK(count[49] == 2);
    CHECK(count[4] == 2);
    CHECK(count[5] == 2);
}

TEST_CASE("Native R2000 DWG: thousands of entities span several map pages") {
    DxfDocument d;
    d.add_layer("BIG", 6);
    for (int i = 0; i < 4000; ++i) d.lines3.push_back({"BIG", {double(i), 0, 0}, {double(i), 1, 0}});
    const DwgImage img = build_dwg(d);
    CHECK(img.entities == 4000);
    const Parsed p = parse(img.bytes);
    CHECK(p.map_pages > 1);
    CHECK(p.types.size() > 4000);
}

TEST_CASE("Native R2000 DWG: polylines past 20000 vertices split at shared joints") {
    auto polyline = [](int n, bool close) {
        chudware::doc::Polyline pts;
        for (int i = 0; i < n; ++i) pts.push_back({i * 0.01, (i % 7) * 0.5});
        if (close) pts.push_back(pts.front());
        DxfDocument d;
        d.polylines.push_back({"0", pts, 0.0, ""});
        return build_dwg(d).entities;
    };
    CHECK(polyline(20000, false) == 1);
    CHECK(polyline(20001, false) == 2);  // 20000 + 2, sharing one joint vertex
    CHECK(polyline(39999, false) == 2);  // 20000 + 20000
    CHECK(polyline(40000, false) == 3);
    CHECK(polyline(20000, true) == 1);   // closed: the repeated first point is dropped
    CHECK(polyline(20001, true) == 2);   // 20002 points as open chunks
}

TEST_CASE("Native R2000 DWG: input R2000 cannot carry is refused by name") {
    auto message = [](const DxfDocument& d) {
        try {
            build_dwg(d);
        } catch (const std::runtime_error& e) {
            return std::string(e.what());
        }
        return std::string();
    };
    {
        DxfDocument d;
        d.polylines.push_back({"A", {{0, 0}, {1, 1}}, 0.0, "PHANTOM"});
        CHECK(message(d).find("PHANTOM") != std::string::npos);
    }
    {
        DxfDocument d;
        d.add_layer("BAD", 0);
        d.polylines.push_back({"BAD", {{0, 0}, {1, 1}}, 0.0, ""});
        CHECK(message(d).find("BAD") != std::string::npos);
    }
    {
        DxfDocument d;
        std::string sat = kSat;
        sat.replace(0, 3, "700");
        d.acis.push_back({"PART", sat, true, 700});
        const std::string m = message(d);
        CHECK(m.find("700") != std::string::npos);
        CHECK(m.find("PART") != std::string::npos);
    }
    {
        DxfDocument d;
        d.acis.push_back({"PART", "400 0 1 0 \nbody $-1 $-1 $-1 $-1 #\n", true, 400});
        CHECK(message(d).find("End-of-ACIS-data") != std::string::npos);
    }
    {
        DxfDocument d;
        d.acis.push_back({"PART", std::string(kSat) + "\xC3\xA9", true, 400});
        CHECK(message(d).find("non-ASCII") != std::string::npos);
    }
    {
        DxfDocument d;
        DxfArc a;
        a.layer = "AXES";
        a.radius = 1.0;
        a.circle = true;
        a.normal = {0.0, 0.0, 0.0};
        d.arcs.push_back(a);
        const std::string m = message(d);
        CHECK(m.find("invalid extrusion") != std::string::npos);
        CHECK(m.find("AXES") != std::string::npos);
    }
}

TEST_CASE("Native R2000 DWG re-reads entities, layers and SAT text with LibreDWG") {
    if (libredwg_tool("dwg2dxf").empty()) {
        MESSAGE("LibreDWG dwg2dxf not installed (scripts/provision_libredwg.py); skipped");
        return;
    }
    DxfDocument d = drawing();
    d.acis.push_back({"PART", kSat, true, 400});
    d.acis.push_back({"SHEET", kSat, false, 400});
    const fs::path dir = fs::temp_directory_path() / "chudware_test_dwg_write";
    fs::create_directories(dir);
    const fs::path dwg = dir / "native.dwg", back = dir / "native_back.dxf";
    write_dwg(dwg.string(), d);  // itself requires every entity back from dwg2dxf
    REQUIRE(run_libredwg("dwg2dxf", {"-y", "-o", back.string(), dwg.string()}));
    // Walk the read-back DXF: entity types per layer, layer linetypes, SAT group lines.
    std::ifstream in(back);
    std::string code, value, entity, section, table_layer;
    std::map<std::string, int> types;
    std::map<std::string, std::string> layer_ltype;
    std::vector<std::string> sat_lines;
    bool want_section = false;
    auto trim = [](std::string s) {
        s.erase(0, s.find_first_not_of(" \r"));
        s.erase(s.find_last_not_of(" \r") + 1);
        return s;
    };
    int solids_seen = 0;
    while (std::getline(in, code) && std::getline(in, value)) {
        const int c = std::stoi(trim(code));
        if (c != 1 && c != 3) value = trim(value);
        else if (!value.empty() && value.back() == '\r') value.pop_back();
        if (c == 0) {
            entity = value;
            if (value == "SECTION") want_section = true;
            if (section == "ENTITIES" && value != "ENDSEC") ++types[value];
            if (value == "3DSOLID") ++solids_seen;
            continue;
        }
        if (want_section && c == 2) { section = value; want_section = false; continue; }
        if (section == "TABLES" && entity == "LAYER") {
            if (c == 2) table_layer = value;
            if (c == 6) layer_ltype[table_layer] = value;
        }
        if (section == "ENTITIES" && entity == "3DSOLID" && solids_seen == 1) {
            if (c == 1) sat_lines.push_back(value);
            if (c == 3 && !sat_lines.empty()) sat_lines.back() += value;
        }
    }
    CHECK(types["LWPOLYLINE"] == 2);
    CHECK(types["ARC"] == 1);
    CHECK(types["CIRCLE"] == 1);
    CHECK(types["TEXT"] == 1);
    CHECK(types["SOLID"] == 1);
    CHECK(types["LINE"] == 1);
    CHECK(types["3DFACE"] == 1);
    CHECK(types["3DSOLID"] == 1);
    CHECK(types["BODY"] == 1);
    CHECK(upper_case_equal(layer_ltype["HIDDEN"], "DASHED"));
    CHECK(upper_case_equal(layer_ltype["CENTER"], "CENTER"));
    // DXF SAT "encryption": byte c > 32 is stored as 159 - c ("^ " escapes '^').
    std::string sat;
    for (const std::string& line : sat_lines) {
        for (std::size_t i = 0; i < line.size(); ++i) {
            const auto ch = static_cast<unsigned char>(line[i]);
            if (ch == '^' && i + 1 < line.size() && line[i + 1] == ' ') {
                sat.push_back('A');
                ++i;
            } else {
                sat.push_back(ch <= 32 ? static_cast<char>(ch) : static_cast<char>(159 - ch));
            }
        }
        sat.push_back('\n');
    }
    CHECK(sat == kSat);
    fs::remove_all(dir);
}

TEST_CASE("Native R2000 DWG keeps a tilted circle's OCS extrusion") {
    // Radius 2 in the WCS plane y = 4 around (10, 4, 7): extrusion +Y, Arbitrary Axis frame
    // X = (-1,0,0), Y = (0,0,1), so the OCS centre is (-10, 7, 4).
    DxfDocument d;
    DxfArc a;
    a.layer = "AXES";
    a.center = {-10.0, 7.0};
    a.z = 4.0;
    a.radius = 2.0;
    a.circle = true;
    a.normal = {0.0, 2.0, 0.0};
    d.arcs.push_back(a);
    a.circle = false;
    a.start_deg = 0.0;
    a.end_deg = 90.0;
    d.arcs.push_back(a);
    CHECK(build_dwg(d).entities == 2);
    if (libredwg_tool("dwg2dxf").empty()) {
        MESSAGE("LibreDWG dwg2dxf not installed (scripts/provision_libredwg.py); read-back skipped");
        return;
    }
    const fs::path dir = fs::temp_directory_path() / "chudware_test_dwg_ocs";
    fs::create_directories(dir);
    const fs::path dwg = dir / "ocs.dwg", back = dir / "ocs_back.dxf";
    write_dwg(dwg.string(), d);
    REQUIRE(run_libredwg("dwg2dxf", {"-y", "-o", back.string(), dwg.string()}));
    std::ifstream in(back);
    std::string code, value, entity, section;
    bool want_section = false;
    std::map<std::string, std::map<int, double>> seen;
    auto trim = [](std::string s) {
        s.erase(0, s.find_first_not_of(" \r"));
        s.erase(s.find_last_not_of(" \r") + 1);
        return s;
    };
    while (std::getline(in, code) && std::getline(in, value)) {
        const int c = std::stoi(trim(code));
        value = trim(value);
        if (c == 0) { entity = value; want_section = value == "SECTION"; continue; }
        if (want_section && c == 2) { section = value; want_section = false; continue; }
        if (section == "ENTITIES" && (entity == "CIRCLE" || entity == "ARC") &&
            (c == 10 || c == 20 || c == 30 || c == 40 || c == 210 || c == 220 || c == 230))
            seen[entity].emplace(c, std::stod(value));
    }
    for (const char* type : {"CIRCLE", "ARC"}) {
        auto& t = seen[type];
        CHECK_MESSAGE(t[10] == doctest::Approx(-10.0), type);
        CHECK_MESSAGE(t[20] == doctest::Approx(7.0), type);
        CHECK_MESSAGE(t[30] == doctest::Approx(4.0), type);
        CHECK_MESSAGE(t[40] == doctest::Approx(2.0), type);
        CHECK_MESSAGE(t[210] == doctest::Approx(0.0), type);
        CHECK_MESSAGE(t[220] == doctest::Approx(1.0), type);
        CHECK_MESSAGE(t[230] == doctest::Approx(0.0), type);
    }
    fs::remove_all(dir);
}
