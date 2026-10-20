// ACIS reader suite (doc/acis_read.h): SAT/SAB decoding, DXF SAT decryption, topology and
// geometry reconstruction. Locks the consumer-visible contract:
//   * an AutoCAD-encrypted DXF SAT payload decrypts to the exact plain text ('A' <-> "^ ");
//   * a closed ACIS lump comes out as a valid TopoDS_Solid with positive, exact volume
//     (SAT 7.0 layout with history ids / pattern pointers / edge parameters, SAT 4.00 layout
//     without them, SAB binary with ENTITY_TYPE_EX chains and tagged vectors/booleans);
//   * the body transform (scale + translation) is applied;
//   * sheet bodies (double-sided faces) come out as shells, not solids;
//   * unsupported geometry and corrupt binary data are reported by name, never dropped.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include <BRepBndLib.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopoDS_Shape.hxx>

#include "doc/acis_read.h"

using chudware::doc::AcisReadResult;
using chudware::doc::decrypt_dxf_sat;
using chudware::doc::read_acis;

namespace {

constexpr double kPi = 3.14159265358979323846;

double volume(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g, 1e-9, true);
    return g.Mass();
}

double area(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::SurfaceProperties(s, g);
    return g.Mass();
}

bool rel_close(double a, double b, double tol = 1e-6) {
    return std::abs(a - b) <= tol * std::max(std::abs(a), std::abs(b));
}

// AutoCAD's DXF SAT cipher: c -> 159 - c for c > 32; the encrypted '^' (plain 'A') is
// written as the DXF caret escape "^ ".
std::string encrypt_line(const std::string& plain) {
    std::string out;
    for (unsigned char c : plain) {
        if (c <= 32) {
            out += static_cast<char>(c);
            continue;
        }
        const char e = static_cast<char>(159 - c);
        out += e;
        if (e == '^') out += ' ';
    }
    return out;
}

// 10x10x10 cube with a radius-2 through hole along z, hand-written SAT 7.0:
// V = 1000 - 40 pi.
const char* kCubeHole700 = R"SAT(700 0 1 0 
@12 hand-written @12 ACIS 7.0 NT @24 Sun Oct 11 12:00:00 2026 
1 9.9999999999999995e-007 1e-010 
body $-1 -1 $-1 $1 $-1 $-1 #
lump $-1 -1 $-1 $-1 $2 $0 #
shell $-1 -1 $-1 $-1 $-1 $58 $-1 $1 #
vertex $-1 -1 $-1 $31 $4 #
point $-1 -1 $-1 0.0 0.0 0.0 #
vertex $-1 -1 $-1 $27 $6 #
point $-1 -1 $-1 0.0 0.0 10.0 #
vertex $-1 -1 $-1 $29 $8 #
point $-1 -1 $-1 0.0 10.0 0.0 #
vertex $-1 -1 $-1 $27 $10 #
point $-1 -1 $-1 0.0 10.0 10.0 #
vertex $-1 -1 $-1 $36 $12 #
point $-1 -1 $-1 10.0 0.0 0.0 #
vertex $-1 -1 $-1 $40 $14 #
point $-1 -1 $-1 10.0 0.0 10.0 #
vertex $-1 -1 $-1 $36 $16 #
point $-1 -1 $-1 10.0 10.0 0.0 #
vertex $-1 -1 $-1 $38 $18 #
point $-1 -1 $-1 10.0 10.0 10.0 #
vertex $-1 -1 $-1 $23 $20 #
point $-1 -1 $-1 7.0 5.0 0.0 #
vertex $-1 -1 $-1 $25 $22 #
point $-1 -1 $-1 7.0 5.0 10.0 #
edge $-1 -1 $-1 $19 0.0 $19 6.283185307179586 $91 $24 forward @7 unknown #
ellipse-curve $-1 -1 $-1 5.0 5.0 0.0 0 0 1 2 0 0 1 I I #
edge $-1 -1 $-1 $21 0.0 $21 6.283185307179586 $98 $26 forward @7 unknown #
ellipse-curve $-1 -1 $-1 5.0 5.0 10.0 0 0 1 2 0 0 1 I I #
edge $-1 -1 $-1 $5 0.0 $9 10.0 $66 $28 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 0.0 10.0 0.0 1.0 0.0 I I #
edge $-1 -1 $-1 $9 0.0 $7 10.0 $67 $30 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 10.0 10.0 0.0 0.0 -1.0 I I #
edge $-1 -1 $-1 $7 0.0 $3 10.0 $68 $32 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 10.0 0.0 0.0 -1.0 0.0 I I #
edge $-1 -1 $-1 $3 0.0 $5 10.0 $69 $34 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 0.0 0.0 0.0 0.0 1.0 I I #
plane-surface $-1 -1 $-1 0.0 0.0 0.0 -1.0 0.0 0.0 0.0 1.0 0.0 forward_v I I I I #
edge $-1 -1 $-1 $11 0.0 $15 10.0 $71 $37 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 0.0 0.0 0.0 1.0 0.0 I I #
edge $-1 -1 $-1 $15 0.0 $17 10.0 $72 $39 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 10.0 0.0 0.0 0.0 1.0 I I #
edge $-1 -1 $-1 $17 0.0 $13 10.0 $73 $41 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 10.0 10.0 0.0 -1.0 0.0 I I #
edge $-1 -1 $-1 $13 0.0 $11 10.0 $74 $43 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 0.0 10.0 0.0 0.0 -1.0 I I #
plane-surface $-1 -1 $-1 10.0 0.0 0.0 1.0 0.0 0.0 0.0 1.0 0.0 forward_v I I I I #
edge $-1 -1 $-1 $3 0.0 $11 10.0 $76 $46 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 0.0 0.0 1.0 0.0 0.0 I I #
edge $-1 -1 $-1 $13 0.0 $5 10.0 $78 $48 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 0.0 10.0 -1.0 0.0 0.0 I I #
plane-surface $-1 -1 $-1 0.0 0.0 0.0 0.0 -1.0 0.0 0.0 0.0 1.0 forward_v I I I I #
edge $-1 -1 $-1 $9 0.0 $17 10.0 $81 $51 forward @7 unknown #
straight-curve $-1 -1 $-1 0.0 10.0 10.0 1.0 0.0 0.0 I I #
edge $-1 -1 $-1 $15 0.0 $7 10.0 $83 $53 forward @7 unknown #
straight-curve $-1 -1 $-1 10.0 10.0 0.0 -1.0 0.0 0.0 I I #
plane-surface $-1 -1 $-1 0.0 10.0 0.0 0.0 1.0 0.0 0.0 0.0 1.0 forward_v I I I I #
plane-surface $-1 -1 $-1 0.0 0.0 0.0 0.0 0.0 -1.0 1.0 0.0 0.0 forward_v I I I I #
plane-surface $-1 -1 $-1 0.0 0.0 10.0 0.0 0.0 1.0 1.0 0.0 0.0 forward_v I I I I #
cone-surface $-1 -1 $-1 5 5 0 0 0 1 2 0 0 1 I I 0 1 2 forward I I I I #
face $-1 -1 $-1 $59 $65 $2 $-1 $35 forward single #
face $-1 -1 $-1 $60 $70 $2 $-1 $44 forward single #
face $-1 -1 $-1 $61 $75 $2 $-1 $49 forward single #
face $-1 -1 $-1 $62 $80 $2 $-1 $54 forward single #
face $-1 -1 $-1 $63 $85 $2 $-1 $55 forward single #
face $-1 -1 $-1 $64 $92 $2 $-1 $56 forward single #
face $-1 -1 $-1 $-1 $99 $2 $-1 $57 reversed single #
loop $-1 -1 $-1 $-1 $66 $58 #
coedge $-1 -1 $-1 $67 $69 $97 $27 forward $65 $-1 #
coedge $-1 -1 $-1 $68 $66 $84 $29 forward $65 $-1 #
coedge $-1 -1 $-1 $69 $67 $90 $31 forward $65 $-1 #
coedge $-1 -1 $-1 $66 $68 $79 $33 forward $65 $-1 #
loop $-1 -1 $-1 $-1 $71 $59 #
coedge $-1 -1 $-1 $72 $74 $88 $36 forward $70 $-1 #
coedge $-1 -1 $-1 $73 $71 $82 $38 forward $70 $-1 #
coedge $-1 -1 $-1 $74 $72 $95 $40 forward $70 $-1 #
coedge $-1 -1 $-1 $71 $73 $77 $42 forward $70 $-1 #
loop $-1 -1 $-1 $-1 $76 $60 #
coedge $-1 -1 $-1 $77 $79 $89 $45 forward $75 $-1 #
coedge $-1 -1 $-1 $78 $76 $74 $42 reversed $75 $-1 #
coedge $-1 -1 $-1 $79 $77 $94 $47 forward $75 $-1 #
coedge $-1 -1 $-1 $76 $78 $69 $33 reversed $75 $-1 #
loop $-1 -1 $-1 $-1 $81 $61 #
coedge $-1 -1 $-1 $82 $84 $96 $50 forward $80 $-1 #
coedge $-1 -1 $-1 $83 $81 $72 $38 reversed $80 $-1 #
coedge $-1 -1 $-1 $84 $82 $87 $52 forward $80 $-1 #
coedge $-1 -1 $-1 $81 $83 $67 $29 reversed $80 $-1 #
loop $-1 -1 $-1 $86 $87 $62 #
loop $-1 -1 $-1 $-1 $91 $62 #
coedge $-1 -1 $-1 $88 $90 $83 $52 reversed $85 $-1 #
coedge $-1 -1 $-1 $89 $87 $71 $36 reversed $85 $-1 #
coedge $-1 -1 $-1 $90 $88 $76 $45 reversed $85 $-1 #
coedge $-1 -1 $-1 $87 $89 $68 $31 reversed $85 $-1 #
coedge $-1 -1 $-1 $91 $91 $101 $23 forward $86 $-1 #
loop $-1 -1 $-1 $93 $94 $63 #
loop $-1 -1 $-1 $-1 $98 $63 #
coedge $-1 -1 $-1 $95 $97 $78 $47 reversed $92 $-1 #
coedge $-1 -1 $-1 $96 $94 $73 $40 reversed $92 $-1 #
coedge $-1 -1 $-1 $97 $95 $81 $50 reversed $92 $-1 #
coedge $-1 -1 $-1 $94 $96 $66 $27 reversed $92 $-1 #
coedge $-1 -1 $-1 $98 $98 $102 $25 reversed $93 $-1 #
loop $-1 -1 $-1 $100 $101 $64 #
loop $-1 -1 $-1 $-1 $102 $64 #
coedge $-1 -1 $-1 $101 $101 $91 $23 reversed $99 $-1 #
coedge $-1 -1 $-1 $102 $102 $98 $25 forward $100 $-1 #
End-of-ACIS-data 
)SAT";

// Minimal SAB writer for the binary-format tests.
struct Sab {
    std::string b;
    void raw32(std::int32_t v) {
        for (int i = 0; i < 4; ++i) b += static_cast<char>((static_cast<std::uint32_t>(v) >> (8 * i)) & 0xFF);
    }
    void str(const std::string& s) {
        b += '\x07';
        b += static_cast<char>(s.size());
        b += s;
    }
    void dbl(double d) {
        b += '\x06';
        std::uint64_t bits;
        std::memcpy(&bits, &d, 8);
        for (int i = 0; i < 8; ++i) b += static_cast<char>((bits >> (8 * i)) & 0xFF);
    }
    void i32(std::int32_t v) {
        b += '\x04';
        raw32(v);
    }
    void ptr(std::int32_t v) {
        b += '\x0C';
        raw32(v);
    }
    void logical(bool t) { b += t ? '\x0A' : '\x0B'; }
    void vec(char tag, double x, double y, double z) {
        b += tag;
        for (double d : {x, y, z}) {
            std::uint64_t bits;
            std::memcpy(&bits, &d, 8);
            for (int i = 0; i < 8; ++i) b += static_cast<char>((bits >> (8 * i)) & 0xFF);
        }
    }
    void type(const std::vector<std::string>& parts) {
        for (std::size_t i = 0; i < parts.size(); ++i) {
            b += i + 1 < parts.size() ? '\x0E' : '\x0D';
            b += static_cast<char>(parts[i].size());
            b += parts[i];
        }
    }
    void end() { b += '\x11'; }
    void header() {
        b = "ASM BinaryFile4";
        raw32(22300);
        raw32(0);
        raw32(1);
        raw32(0);
        str("test");
        str("ASM 228.0.0.65535 NT");
        str("Sun Oct 11 12:00:00 2026");
        dbl(25.4);
        dbl(1e-6);
        dbl(1e-10);
    }
    // >= 7.0 record head: $attrib, history id[, $pattern]
    void head(bool pattern = true) {
        ptr(-1);
        i32(-1);
        if (pattern) ptr(-1);
    }
    void open() { b += '\x0F'; }
    void close() { b += '\x10'; }
    void enumeration(std::int32_t v) {
        b += '\x15';
        raw32(v);
    }
};

// One-face sphere body (centre (1,2,3), radius 5) as SAB, ASM 228 style.
std::string sab_sphere() {
    Sab s;
    s.header();
    s.type({"asmheader"});  // 0
    s.ptr(-1);
    s.i32(-1);
    s.str("228.0.0.65535");
    s.end();
    s.type({"body"});  // 1
    s.head();
    s.ptr(2);
    s.ptr(-1);
    s.ptr(-1);
    s.end();
    s.type({"lump"});  // 2
    s.head();
    s.ptr(-1);
    s.ptr(3);
    s.ptr(1);
    s.end();
    s.type({"shell"});  // 3
    s.head();
    s.ptr(-1);
    s.ptr(-1);
    s.ptr(4);
    s.ptr(-1);
    s.ptr(2);
    s.end();
    s.type({"face"});  // 4
    s.head();
    s.ptr(-1);
    s.ptr(-1);
    s.ptr(3);
    s.ptr(-1);
    s.ptr(5);
    s.logical(false);  // forward
    s.logical(false);  // single
    s.end();
    s.type({"sphere", "surface"});  // 5
    s.head();
    s.vec('\x13', 1, 2, 3);
    s.dbl(5.0);
    s.vec('\x14', 1, 0, 0);
    s.vec('\x14', 0, 0, 1);
    for (int i = 0; i < 5; ++i) s.logical(false);  // forward_v, I I I I
    s.end();
    s.type({"End", "of", "ASM", "data"});
    return s.b;
}

// Double-sided 10x10 square sheet in the XY plane whose first edge (0,0,0)-(10,0,0) is an
// intcurve in the layout ASM (AutoCAD 2018+) writes when it re-saves an `exactcur`:
// `{ exact_int_cur <save version> <enum qualifier> nubs ... }` with poles as plain doubles.
std::string sab_sheet_exact_int_cur() {
    Sab s;
    s.header();
    s.type({"asmheader"});  // 0
    s.ptr(-1);
    s.i32(-1);
    s.str("228.0.0.65535");
    s.end();
    s.type({"body"});  // 1
    s.head();
    s.ptr(2);
    s.ptr(-1);
    s.ptr(-1);
    s.end();
    s.type({"lump"});  // 2
    s.head();
    s.ptr(-1);
    s.ptr(3);
    s.ptr(1);
    s.end();
    s.type({"shell"});  // 3
    s.head();
    s.ptr(-1);
    s.ptr(-1);
    s.ptr(4);
    s.ptr(-1);
    s.ptr(2);
    s.end();
    s.type({"face"});  // 4
    s.head();
    s.ptr(-1);
    s.ptr(5);
    s.ptr(3);
    s.ptr(-1);
    s.ptr(6);
    s.logical(false);  // forward
    s.logical(true);   // double
    s.logical(false);  // out
    s.end();
    s.type({"loop"});  // 5
    s.head();
    s.ptr(-1);
    s.ptr(7);
    s.ptr(4);
    s.end();
    s.type({"plane", "surface"});  // 6
    s.head();
    s.vec('\x13', 0, 0, 0);
    s.vec('\x14', 0, 0, 1);
    s.vec('\x14', 1, 0, 0);
    for (int i = 0; i < 5; ++i) s.logical(false);
    s.end();
    for (int i = 0; i < 4; ++i) {  // 7..10 coedges
        s.type({"coedge"});
        s.head();
        s.ptr(7 + (i + 1) % 4);
        s.ptr(7 + (i + 3) % 4);
        s.ptr(-1);
        s.ptr(11 + i);
        s.logical(false);
        s.ptr(5);
        s.i32(0);
        s.ptr(-1);
        s.end();
    }
    for (int i = 0; i < 4; ++i) {  // 11..14 edges
        s.type({"edge"});
        s.head();
        s.ptr(15 + i);
        s.dbl(0.0);
        s.ptr(15 + (i + 1) % 4);
        s.dbl(10.0);
        s.ptr(7 + i);
        s.ptr(19 + i);
        s.logical(false);
        s.str("unknown");
        s.end();
    }
    for (int i = 0; i < 4; ++i) {  // 15..18 vertices
        s.type({"vertex"});
        s.head();
        s.ptr(11 + i);
        s.i32(0);
        s.ptr(23 + i);
        s.end();
    }
    s.type({"intcurve", "curve"});  // 19
    s.head();
    s.logical(false);  // forward
    s.open();
    s.type({"exact_int_cur"});
    s.i32(21500);
    s.enumeration(0);
    s.type({"nubs"});
    s.i32(1);
    s.enumeration(0);  // open
    s.i32(2);
    s.dbl(0.0);
    s.i32(1);
    s.dbl(10.0);
    s.i32(1);
    for (double d : {0.0, 0.0, 0.0, 10.0, 0.0, 0.0}) s.dbl(d);
    s.dbl(0.0);  // fitol
    s.type({"null_surface"});
    s.type({"null_surface"});
    s.type({"nullbs"});
    s.type({"nullbs"});
    s.logical(false);
    s.logical(false);
    for (int i = 0; i < 4; ++i) s.i32(0);
    s.close();
    s.logical(false);  // I
    s.logical(false);  // I
    s.end();
    const double pts[4][3] = {{0, 0, 0}, {10, 0, 0}, {10, 10, 0}, {0, 10, 0}};
    for (int i = 1; i < 4; ++i) {  // 20..22 straight edges i: pts[i] -> pts[i+1]
        const double* a = pts[i];
        const double* b = pts[(i + 1) % 4];
        s.type({"straight", "curve"});
        s.head();
        s.vec('\x13', a[0], a[1], a[2]);
        s.vec('\x14', (b[0] - a[0]) / 10.0, (b[1] - a[1]) / 10.0, (b[2] - a[2]) / 10.0);
        s.logical(false);
        s.logical(false);
        s.end();
    }
    for (const auto& p : pts) {  // 23..26 points
        s.type({"point"});
        s.head();
        s.vec('\x13', p[0], p[1], p[2]);
        s.end();
    }
    s.type({"End", "of", "ASM", "data"});
    return s.b;
}

}  // namespace

TEST_CASE("decrypt_dxf_sat undoes the AutoCAD cipher including the caret escape") {
    const std::vector<std::string> plain = {
        "700 0 1 0 ",
        "@33 Open Design Alliance ACIS Builder @14 ACIS 215.00 NT @24 Sun Oct 11 20:47:05 2026 ",
        "body $-1 -1 $-1 $1 $-1 $-1 #",
        "End-of-ACIS-data "};
    std::vector<std::string> enc;
    std::string expect;
    for (const auto& l : plain) {
        enc.push_back(encrypt_line(l));
        expect += l + "\n";
    }
    CHECK(enc[1].find("^ ") != std::string::npos);   // the 'A's really are escaped
    CHECK(decrypt_dxf_sat(enc) == expect);
    // DXF control escapes are passed through as control characters.
    CHECK(decrypt_dxf_sat({"^I"}) == "\t\n");
}

TEST_CASE("SAT 7.0 cube with a cylindrical hole is an exact valid solid") {
    const AcisReadResult r = read_acis(kCubeHole700);
    REQUIRE(r.bodies.size() == 1);
    CHECK(r.warnings.empty());
    CHECK(r.mm_per_unit == doctest::Approx(1.0));
    const auto& b = r.bodies[0];
    CHECK(b.solid);
    CHECK(b.shape.ShapeType() == TopAbs_SOLID);
    CHECK(BRepCheck_Analyzer(b.shape).IsValid());
    CHECK(rel_close(volume(b.shape), 1000.0 - 40.0 * kPi));
    CHECK(rel_close(area(b.shape), 600.0 - 2.0 * kPi * 4.0 + 2.0 * kPi * 2.0 * 10.0));
}

TEST_CASE("encrypted DXF payload round-trips into the same solid") {
    std::vector<std::string> enc;
    std::string text = kCubeHole700;
    std::size_t p = 0;
    while (p < text.size()) {
        std::size_t e = text.find('\n', p);
        if (e == std::string::npos) e = text.size();
        enc.push_back(encrypt_line(text.substr(p, e - p)));
        p = e + 1;
    }
    const AcisReadResult r = read_acis(decrypt_dxf_sat(enc));
    REQUIRE(r.bodies.size() == 1);
    CHECK(r.bodies[0].solid);
    CHECK(rel_close(volume(r.bodies[0].shape), 1000.0 - 40.0 * kPi));
}

TEST_CASE("SAT 4.00 layout (no history ids, no edge parameters) and loop-less faces") {
    const char* sat =
        "400 0 1 0 \n"
        "4 test 12 ACIS 4.00 NT 24 Sun Oct 11 12:00:00 2026 \n"
        "25.4 9.9999999999999995e-007 1e-010 \n"
        "body $-1 $1 $-1 $-1 #\n"
        "lump $-1 $-1 $2 $0 #\n"
        "shell $-1 $-1 $-1 $3 $-1 $1 #\n"
        "face $-1 $-1 $-1 $2 $-1 $4 forward single #\n"
        "torus-surface $-1 0 0 0 0 0 1 10 2 1 0 0 forward_v I I I I #\n"
        "End-of-ACIS-data \n";
    const AcisReadResult r = read_acis(sat);
    REQUIRE(r.bodies.size() == 1);
    CHECK(r.mm_per_unit == doctest::Approx(25.4));
    CHECK(r.bodies[0].solid);
    CHECK(BRepCheck_Analyzer(r.bodies[0].shape).IsValid());
    CHECK(rel_close(volume(r.bodies[0].shape), 2.0 * kPi * kPi * 10.0 * 4.0));
}

TEST_CASE("body transform: rows, translation and uniform scale are applied") {
    const char* sat =
        "700 0 1 0 \n"
        "@4 test @12 ACIS 7.0 NT @24 Sun Oct 11 12:00:00 2026 \n"
        "1 9.9999999999999995e-007 1e-010 \n"
        "body $-1 -1 $-1 $1 $-1 $5 #\n"
        "lump $-1 -1 $-1 $-1 $2 $0 #\n"
        "shell $-1 -1 $-1 $-1 $-1 $3 $-1 $1 #\n"
        "face $-1 -1 $-1 $-1 $-1 $2 $-1 $4 forward single #\n"
        "sphere-surface $-1 -1 $-1 0 0 0 1 1 0 0 0 0 1 forward_v I I I I #\n"
        "transform $-1 -1 0 1 0 -1 0 0 0 0 1 100 0 0 3 rotate no_reflect no_shear #\n"
        "End-of-ACIS-data \n";
    const AcisReadResult r = read_acis(sat);
    REQUIRE(r.bodies.size() == 1);
    const TopoDS_Shape& s = r.bodies[0].shape;
    CHECK(rel_close(volume(s), 4.0 / 3.0 * kPi * 27.0));
    Bnd_Box bb;
    BRepBndLib::AddOptimal(s, bb, false, false);
    double x0, y0, z0, x1, y1, z1;
    bb.Get(x0, y0, z0, x1, y1, z1);
    CHECK(0.5 * (x0 + x1) == doctest::Approx(100.0).epsilon(1e-6));
    CHECK(x1 - x0 == doctest::Approx(6.0).epsilon(1e-6));
}

TEST_CASE("SAB (ASM BinaryFile4) with ENTITY_TYPE_EX chains, vectors and logicals") {
    const AcisReadResult r = read_acis(sab_sphere());
    REQUIRE(r.bodies.size() == 1);
    CHECK(r.mm_per_unit == doctest::Approx(25.4));
    CHECK(r.bodies[0].solid);
    CHECK(rel_close(volume(r.bodies[0].shape), 4.0 / 3.0 * kPi * 125.0));
}

TEST_CASE("ASM exact_int_cur (save version + enum qualifier) builds its edge") {
    const AcisReadResult r = read_acis(sab_sheet_exact_int_cur());
    CHECK(r.warnings.empty());
    REQUIRE(r.bodies.size() == 1);
    CHECK_FALSE(r.bodies[0].solid);
    CHECK(r.bodies[0].shape.ShapeType() == TopAbs_SHELL);
    CHECK(BRepCheck_Analyzer(r.bodies[0].shape).IsValid());
    CHECK(area(r.bodies[0].shape) == doctest::Approx(100.0));
}

TEST_CASE("an unknown SAB tag is reported by name and position") {
    Sab s;
    s.header();
    s.type({"body"});
    s.b += '\x1f';
    try {
        read_acis(s.b);
        FAIL("expected std::runtime_error");
    } catch (const std::runtime_error& e) {
        const std::string msg = e.what();
        CHECK(msg.find("unknown SAB tag 0x1f") != std::string::npos);
        CHECK(msg.find("body") != std::string::npos);
    }
}

TEST_CASE("a double-sided face body is a sheet (shell), not a solid") {
    const char* sat =
        "700 0 1 0 \n"
        "@4 test @12 ACIS 7.0 NT @24 Sun Oct 11 12:00:00 2026 \n"
        "1 9.9999999999999995e-007 1e-010 \n"
        "body $-1 -1 $-1 $1 $-1 $-1 #\n"                         // 0
        "lump $-1 -1 $-1 $-1 $2 $0 #\n"                          // 1
        "shell $-1 -1 $-1 $-1 $-1 $3 $-1 $1 #\n"                 // 2
        "face $-1 -1 $-1 $-1 $4 $2 $-1 $5 forward double out #\n" // 3
        "loop $-1 -1 $-1 $-1 $6 $3 #\n"                          // 4
        "plane-surface $-1 -1 $-1 0 0 0 0 0 1 1 0 0 forward_v I I I I #\n"  // 5
        "coedge $-1 -1 $-1 $7 $9 $-1 $10 forward $4 $-1 #\n"     // 6
        "coedge $-1 -1 $-1 $8 $6 $-1 $11 forward $4 $-1 #\n"     // 7
        "coedge $-1 -1 $-1 $9 $7 $-1 $12 forward $4 $-1 #\n"     // 8
        "coedge $-1 -1 $-1 $6 $8 $-1 $13 forward $4 $-1 #\n"     // 9
        "edge $-1 -1 $-1 $14 0 $15 10 $6 $18 forward @7 unknown #\n"   // 10
        "edge $-1 -1 $-1 $15 0 $16 10 $7 $19 forward @7 unknown #\n"   // 11
        "edge $-1 -1 $-1 $16 0 $17 10 $8 $20 forward @7 unknown #\n"   // 12
        "edge $-1 -1 $-1 $17 0 $14 10 $9 $21 forward @7 unknown #\n"   // 13
        "vertex $-1 -1 $-1 $10 $22 #\n"                          // 14
        "vertex $-1 -1 $-1 $11 $23 #\n"                          // 15
        "vertex $-1 -1 $-1 $12 $24 #\n"                          // 16
        "vertex $-1 -1 $-1 $13 $25 #\n"                          // 17
        "straight-curve $-1 -1 $-1 0 0 0 1 0 0 I I #\n"          // 18
        "straight-curve $-1 -1 $-1 10 0 0 0 1 0 I I #\n"         // 19
        "straight-curve $-1 -1 $-1 10 10 0 -1 0 0 I I #\n"       // 20
        "straight-curve $-1 -1 $-1 0 10 0 0 -1 0 I I #\n"        // 21
        "point $-1 -1 $-1 0 0 0 #\n"                             // 22
        "point $-1 -1 $-1 10 0 0 #\n"                            // 23
        "point $-1 -1 $-1 10 10 0 #\n"                           // 24
        "point $-1 -1 $-1 0 10 0 #\n"                            // 25
        "End-of-ACIS-data \n";
    const AcisReadResult r = read_acis(sat);
    REQUIRE(r.bodies.size() == 1);
    CHECK_FALSE(r.bodies[0].solid);
    CHECK(r.bodies[0].shape.ShapeType() == TopAbs_SHELL);
    CHECK(BRepCheck_Analyzer(r.bodies[0].shape).IsValid());
    CHECK(area(r.bodies[0].shape) == doctest::Approx(100.0));
}

TEST_CASE("unsupported geometry is named in the error, not dropped silently") {
    const char* sat =
        "700 0 1 0 \n"
        "@4 test @12 ACIS 7.0 NT @24 Sun Oct 11 12:00:00 2026 \n"
        "1 9.9999999999999995e-007 1e-010 \n"
        "body $-1 -1 $-1 $1 $-1 $-1 #\n"
        "lump $-1 -1 $-1 $-1 $2 $0 #\n"
        "shell $-1 -1 $-1 $-1 $-1 $3 $-1 $1 #\n"
        "face $-1 -1 $-1 $-1 $-1 $2 $-1 $4 forward single #\n"
        "meshsurf-surface $-1 -1 $-1 1 2 3 #\n"
        "End-of-ACIS-data \n";
    try {
        read_acis(sat);
        FAIL("expected std::runtime_error");
    } catch (const std::runtime_error& e) {
        CHECK(std::string(e.what()).find("unsupported surface type 'meshsurf'") != std::string::npos);
    }
    CHECK_THROWS_AS(read_acis("not acis at all"), std::runtime_error);
}

TEST_CASE("native input boundary: SAT counted strings reject overflow and truncation") {
    for (const std::string& token : {
             std::string("@18446744073709551594 x"),
             std::string("@184467440737095516160 x"),
             std::string("@99 x"),
             std::string("@1x")}) {
        const std::string sat = "700 0 1 0\n1 1e-6 1e-10\nbody " + token + " #\n";
        try {
            (void)read_acis(sat);
            FAIL("malformed counted string was accepted");
        } catch (const std::runtime_error& error) {
            CHECK(std::string(error.what()).find("SAT counted string") != std::string::npos);
        }
    }
}

TEST_CASE("native input boundary: SAT counted string retains embedded record markers") {
    std::string sat = kCubeHole700;
    const auto at = sat.find("@7 unknown");
    REQUIRE(at != std::string::npos);
    sat.replace(at, std::strlen("@7 unknown"), "@8 un#known");
    const auto result = read_acis(sat);
    REQUIRE(result.bodies.size() == 1);
    CHECK(BRepCheck_Analyzer(result.bodies[0].shape).IsValid());
    CHECK(volume(result.bodies[0].shape) == doctest::Approx(1000.0 - 40.0 * kPi));
}
