// ACIS SAT/SAB reader -> OCCT B-rep. See acis_read.h for the contract.
//
// Pipeline: SAT text or SAB bytes -> one normalised token stream per record (Model) ->
// typed topology records -> OCCT geometry (exact for analytic types, the stored B-spline
// approximation for procedural splines) -> shared vertices/edges -> faces/shells/solids ->
// ShapeFix healing (pcurves, seams, tolerances, orientation) -> body transform.
//
// Format notes. Record layouts follow the public ACIS save-file format as observed in
// AutoCAD (ASM 228), ODA (4.00 / 215.00) and Inventor (7.0) output:
//  * every record: `$attrib`; >= 7.0 adds an integer history id; topology and geometry
//    records (not attributes / transform / asmheader) >= 7.0 add a `$pattern` pointer;
//  * edges >= 5.0 carry start/end curve parameters and a convexity string; vertices and
//    coedges may carry an extra integer (SAB always, SAT sometimes) that is skipped;
//  * spline surfaces / intcurves carry a subtype block `{ name ... }`; blocks are numbered
//    in file order and `{ ref N }` reuses block N. Exact splines and procedural subtypes
//    store an approximating bs3 B-spline; ACIS knot vectors omit the two outermost knots.
//    When only a "summary" is stored, intcurves are rebuilt from their pcurve on a defining
//    surface and constant-radius rolling-ball blends (rbblnsur) are evaluated from their
//    supports, offsets and defining curve; anything else is reported by name.
// The SAB tag table and the DXF SAT decryption rule are ported from ezdxf (MIT License,
// Copyright (c) 2014-2026 Manfred Moitzi; ezdxf/acis/sab.py, acis/const.py,
// tools/crypt.py).

#include "doc/acis_read.h"

#include <BRepBuilderAPI_GTransform.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepLib.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <ElCLib.hxx>
#include <GProp_GProps.hxx>
#include <Geom2d_BSplineCurve.hxx>
#include <Geom2d_Curve.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <GeomConvert.hxx>
#include <GeomLProp_SLProps.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Geom_BSplineSurface.hxx>
#include <Geom_Circle.hxx>
#include <Geom_ConicalSurface.hxx>
#include <Geom_CylindricalSurface.hxx>
#include <Geom_Ellipse.hxx>
#include <Geom_Line.hxx>
#include <Geom_Plane.hxx>
#include <GeomAPI_PointsToBSplineSurface.hxx>
#include <Geom_RectangularTrimmedSurface.hxx>
#include <Geom_TrimmedCurve.hxx>
#include <Geom_SphericalSurface.hxx>
#include <Geom_SurfaceOfLinearExtrusion.hxx>
#include <Geom_ToroidalSurface.hxx>
#include <Precision.hxx>
#include <ShapeFix_Shape.hxx>
#include <Standard_Failure.hxx>
#include <TColStd_Array1OfInteger.hxx>
#include <TColStd_Array1OfReal.hxx>
#include <TColStd_Array2OfReal.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TColgp_Array1OfPnt2d.hxx>
#include <TColgp_Array2OfPnt.hxx>
#include <TopExp_Explorer.hxx>
#include <TopExp.hxx>
#include <TopLoc_Location.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Iterator.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shell.hxx>
#include <TopoDS_Solid.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_GTrsf.hxx>
#include <gp_Mat.hxx>
#include <gp_Trsf.hxx>

#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace chudware::doc {
namespace {

constexpr double kPi = 3.14159265358979323846;

// ---------------------------------------------------------------------------------------------
// Normalised token stream shared by SAT and SAB.

enum class TK : std::uint8_t { Num, Ptr, Str, Word, Bool, Enum, Vec, Vec2, Open, Close };

struct Tok {
    TK k = TK::Num;
    bool integral = false;                    // Num: written as an integer (SAT) / int tag (SAB)
    bool b = false;                           // Bool (SAB 0x0A / 0x0B)
    std::int64_t i = 0;                       // integral Num / Ptr / Enum
    double d = 0.0;                           // Num
    std::array<double, 3> v{0.0, 0.0, 0.0};   // Vec / Vec2
    std::string s;                            // Str / Word
};

struct Rec {
    std::string type;   // full ACIS type, e.g. "cone-surface", "tvertex-vertex"
    std::string base;   // last '-' component: "surface", "vertex", "attrib", ...
    std::string kind;   // first component: "cone", "tvertex", "body", ...
    std::vector<Tok> t;
};

struct Model {
    int version = 0;
    bool binary = false;
    double units = 0.0;
    double res_tol = 1e-6;
    std::vector<Rec> recs;
    // Subtype blocks in file order (the `ref N` index) -> (record, index of the Open token).
    std::vector<std::pair<int, std::size_t>> subtypes;
    std::map<std::pair<int, std::size_t>, int> subtype_index;
};

[[noreturn]] void fail(const std::string& what) { throw std::runtime_error("ACIS: " + what); }

bool is_end_marker(std::string_view w) {
    return w.rfind("End-of-ACIS-data", 0) == 0 || w.rfind("End-of-ASM-data", 0) == 0 ||
           w.rfind("Begin-of-ACIS-History-data", 0) == 0;
}

void split_type(Rec& r) {
    const auto last = r.type.rfind('-');
    r.base = last == std::string::npos ? r.type : r.type.substr(last + 1);
    const auto first = r.type.find('-');
    r.kind = first == std::string::npos ? r.type : r.type.substr(0, first);
}

bool parse_number(std::string_view w, Tok& out) {
    if (w.empty()) return false;
    const char c0 = w[0];
    if (!(std::isdigit(static_cast<unsigned char>(c0)) || c0 == '-' || c0 == '+' || c0 == '.'))
        return false;
    if (c0 == '+') w.remove_prefix(1);
    if (w.empty()) return false;
    const char* b = w.data();
    const char* e = b + w.size();
    double d = 0.0;
    auto r = std::from_chars(b, e, d);
    if (r.ec != std::errc() || r.ptr != e) return false;
    out.k = TK::Num;
    out.d = d;
    std::int64_t iv = 0;
    auto ri = std::from_chars(b, e, iv);
    out.integral = ri.ec == std::errc() && ri.ptr == e;
    out.i = out.integral ? iv : 0;
    return true;
}

void index_subtypes(Model& m) {
    for (int r = 0; r < static_cast<int>(m.recs.size()); ++r) {
        const auto& t = m.recs[r].t;
        for (std::size_t i = 0; i < t.size(); ++i) {
            if (t[i].k != TK::Open) continue;
            const bool is_ref = i + 1 < t.size() &&
                                (t[i + 1].k == TK::Word || t[i + 1].k == TK::Str) &&
                                t[i + 1].s == "ref";
            if (is_ref) continue;
            m.subtype_index[{r, i}] = static_cast<int>(m.subtypes.size());
            m.subtypes.emplace_back(r, i);
        }
    }
}

// ---------------------------------------------------------------------------------------------
// SAT text.

Model parse_sat(std::string_view s) {
    Model m;
    std::size_t p = 0;
    const std::size_t n = s.size();
    auto next_line = [&]() -> std::string_view {
        while (p < n) {
            std::size_t e = s.find('\n', p);
            if (e == std::string_view::npos) e = n;
            std::string_view line = s.substr(p, e - p);
            p = e < n ? e + 1 : n;
            if (line.find_first_not_of(" \t\r") != std::string_view::npos) return line;
        }
        return {};
    };
    auto words = [](std::string_view line) {
        std::vector<std::string_view> w;
        std::size_t i = 0;
        while (i < line.size()) {
            while (i < line.size() && std::isspace(static_cast<unsigned char>(line[i]))) ++i;
            std::size_t b = i;
            while (i < line.size() && !std::isspace(static_cast<unsigned char>(line[i]))) ++i;
            if (i > b) w.push_back(line.substr(b, i - b));
        }
        return w;
    };

    // Line 1: "<version> <records> <bodies> <flags>".
    {
        auto w = words(next_line());
        Tok t;
        if (w.empty() || !parse_number(w[0], t) || !t.integral)
            fail("not SAT data (first line has no version number)");
        m.version = static_cast<int>(t.i);
    }
    // Line 2: product / ACIS version / date as counted strings ("@N text" or "N text");
    // line 3: "<mm per unit> <resabs> <resnor>". Both are absent in the oldest files.
    auto all_numbers = [](const std::vector<std::string_view>& w) {
        Tok t;
        for (auto x : w)
            if (!parse_number(x, t)) return false;
        return !w.empty();
    };
    std::size_t save = p;
    auto l2 = words(next_line());
    if (l2.size() == 3 && all_numbers(l2)) {
        p = save;   // no product line; this is the units line
    } else if (!l2.empty()) {
        Tok t;
        std::string_view w0 = l2[0];
        if (!w0.empty() && w0[0] == '@') w0.remove_prefix(1);
        if (!parse_number(w0, t)) p = save;   // records start right away
    }
    save = p;
    auto l3 = words(next_line());
    if (l3.size() >= 2 && all_numbers(l3)) {
        Tok t;
        parse_number(l3[0], t);
        m.units = t.d;
        parse_number(l3[1], t);
        if (t.d > 0.0) m.res_tol = t.d;
    } else {
        p = save;
    }

    // Records: whitespace-separated tokens, '#' terminates a record, "@N " introduces an
    // N-character string that may contain spaces or '#'.
    Rec cur;
    bool in_rec = false;
    bool finished = false;
    while (p < n && !finished) {
        while (p < n && std::isspace(static_cast<unsigned char>(s[p]))) ++p;
        if (p >= n) break;
        if (in_rec && s[p] == '@' && p + 1 < n && std::isdigit(static_cast<unsigned char>(s[p + 1]))) {
            std::size_t q = p + 1;
            std::size_t count = 0;
            while (q < n && std::isdigit(static_cast<unsigned char>(s[q])))
                count = count * 10 + static_cast<std::size_t>(s[q++] - '0');
            if (q < n && s[q] == ' ') ++q;
            Tok t;
            t.k = TK::Str;
            t.s = std::string(s.substr(q, std::min(count, n - q)));
            cur.t.push_back(std::move(t));
            p = std::min(n, q + count);
            continue;
        }
        std::size_t b = p;
        while (p < n && !std::isspace(static_cast<unsigned char>(s[p]))) ++p;
        std::string_view w = s.substr(b, p - b);
        bool ends = false;
        if (w == "#") {
            ends = true;
            w = {};
        } else if (w.size() > 1 && w.back() == '#') {
            w.remove_suffix(1);
            ends = true;
        }
        if (!w.empty()) {
            if (!in_rec) {
                if (is_end_marker(w)) {
                    finished = true;
                    break;
                }
                Tok seq;
                if (w[0] == '-' && parse_number(w, seq) && seq.integral) continue;   // "-12 face"
                cur = Rec{};
                cur.type = std::string(w);
                in_rec = true;
            } else {
                Tok t;
                if (w[0] == '$' && w.size() > 1 && parse_number(w.substr(1), t) && t.integral) {
                    t.k = TK::Ptr;
                } else if (w == "{") {
                    t = Tok{};
                    t.k = TK::Open;
                } else if (w == "}") {
                    t = Tok{};
                    t.k = TK::Close;
                } else if (!parse_number(w, t)) {
                    t = Tok{};
                    t.k = TK::Word;
                    t.s = std::string(w);
                }
                cur.t.push_back(std::move(t));
            }
        }
        if (ends && in_rec) {
            split_type(cur);
            m.recs.push_back(std::move(cur));
            cur = Rec{};
            in_rec = false;
        }
    }
    if (in_rec)
        fail("SAT record " + std::to_string(m.recs.size()) + " (" + cur.type +
             ") is not terminated by '#'");
    index_subtypes(m);
    return m;
}

// ---------------------------------------------------------------------------------------------
// SAB binary. Tag table (ezdxf/acis/const.py, extended): 0x04 int32, 0x06 double, 0x07/0x08/
// 0x09 string with 1/2/4-byte length, 0x0A true, 0x0B false, 0x0C pointer, 0x0D identifier
// (last type component), 0x0E sub-identifier, 0x0F/0x10 subtype block, 0x11 record end,
// 0x12 literal string (4-byte length), 0x13 position, 0x14 direction, 0x15 enum,
// 0x16 parameter position (2 doubles), 0x17 int64.

constexpr std::string_view kAcisSig = "ACIS BinaryFile";
constexpr std::string_view kAsmSig = "ASM BinaryFile4";

struct SabIn {
    std::string_view s;
    std::size_t p = 0;
    void need(std::size_t k) const {
        if (p + k > s.size()) fail("SAB data truncated at byte " + std::to_string(p));
    }
    std::uint64_t le(std::size_t k) {
        need(k);
        std::uint64_t v = 0;
        for (std::size_t i = 0; i < k; ++i)
            v |= static_cast<std::uint64_t>(static_cast<unsigned char>(s[p + i])) << (8 * i);
        p += k;
        return v;
    }
    std::uint8_t u8() { return static_cast<std::uint8_t>(le(1)); }
    std::int32_t i32() { return static_cast<std::int32_t>(static_cast<std::uint32_t>(le(4))); }
    double f64() {
        const std::uint64_t bits = le(8);
        double d;
        std::memcpy(&d, &bits, 8);
        return d;
    }
    std::string str(std::size_t len) {
        need(len);
        std::string out(s.substr(p, len));
        p += len;
        return out;
    }
};

Model parse_sab(std::string_view s) {
    Model m;
    m.binary = true;
    SabIn in{s, 0};
    if (s.substr(0, kAcisSig.size()) == kAcisSig)
        in.p = kAcisSig.size();
    else if (s.substr(0, kAsmSig.size()) == kAsmSig)
        in.p = kAsmSig.size();
    else
        fail("not SAB data (missing ACIS/ASM BinaryFile signature)");
    m.version = in.i32();
    (void)in.i32();   // record count (may be 0)
    (void)in.i32();   // body count
    (void)in.i32();   // flags
    for (int i = 0; i < 3; ++i) {   // product, ACIS version, date
        const std::uint8_t tag = in.u8();
        if (tag == 0x07) in.str(in.u8());
        else if (tag == 0x08) in.str(static_cast<std::size_t>(in.le(2)));
        else if (tag == 0x09 || tag == 0x12) in.str(static_cast<std::size_t>(in.le(4)));
        else fail("SAB header: expected string tag, got 0x" + std::to_string(tag));
    }
    double hdr[3] = {0.0, 0.0, 0.0};
    for (double& h : hdr) {
        if (in.u8() != 0x06) fail("SAB header: expected double tag (units / tolerances)");
        h = in.f64();
    }
    m.units = hdr[0];
    if (hdr[1] > 0.0) m.res_tol = hdr[1];

    std::vector<std::string> pieces;
    auto join = [&pieces]() {
        std::string out;
        for (std::size_t i = 0; i < pieces.size(); ++i) {
            if (i) out += '-';
            out += pieces[i];
        }
        pieces.clear();
        return out;
    };
    while (in.p < s.size()) {
        Rec r;
        bool typed = false;
        bool stop = false;
        for (;;) {
            if (in.p >= s.size()) {
                if (!typed && r.t.empty() && pieces.empty()) {
                    stop = true;
                    break;
                }
                fail("SAB record " + std::to_string(m.recs.size()) + " (" + r.type +
                     ") truncated at end of data");
            }
            const std::uint8_t tag = in.u8();
            Tok t;
            bool push = true;
            bool end = false;
            switch (tag) {
            case 0x01: case 0x02:
                t.integral = true; t.i = static_cast<std::int8_t>(in.u8()); t.d = double(t.i); break;
            case 0x03:
                t.integral = true; t.i = static_cast<std::int16_t>(in.le(2)); t.d = double(t.i); break;
            case 0x04:
                t.integral = true; t.i = in.i32(); t.d = double(t.i); break;
            case 0x05: {
                const auto bits = static_cast<std::uint32_t>(in.le(4));
                float f;
                std::memcpy(&f, &bits, 4);
                t.d = f;
                break;
            }
            case 0x06: t.d = in.f64(); break;
            case 0x07: t.k = TK::Str; t.s = in.str(in.u8()); break;
            case 0x08: t.k = TK::Str; t.s = in.str(static_cast<std::size_t>(in.le(2))); break;
            case 0x09: case 0x12: t.k = TK::Str; t.s = in.str(static_cast<std::size_t>(in.le(4))); break;
            case 0x0A: t.k = TK::Bool; t.b = true; break;
            case 0x0B: t.k = TK::Bool; t.b = false; break;
            case 0x0C: t.k = TK::Ptr; t.i = in.i32(); break;
            case 0x0D: {
                pieces.push_back(in.str(in.u8()));
                std::string name = join();
                if (!typed) {
                    r.type = std::move(name);
                    typed = true;
                    push = false;
                    if (is_end_marker(r.type)) {
                        stop = true;
                        end = true;
                    }
                } else {
                    t.k = TK::Word;
                    t.s = std::move(name);
                }
                break;
            }
            case 0x0E: pieces.push_back(in.str(in.u8())); push = false; break;
            case 0x0F: t.k = TK::Open; break;
            case 0x10: t.k = TK::Close; break;
            case 0x11: push = false; end = true; break;
            case 0x13: case 0x14:
                t.k = TK::Vec;
                for (double& c : t.v) c = in.f64();
                break;
            case 0x15: t.k = TK::Enum; t.i = in.i32(); break;
            case 0x16:
                t.k = TK::Vec2;
                t.v[0] = in.f64();
                t.v[1] = in.f64();
                break;
            case 0x17:
                t.integral = true; t.i = static_cast<std::int64_t>(in.le(8)); t.d = double(t.i); break;
            default: {
                char hex[8];
                std::snprintf(hex, sizeof hex, "0x%02x", tag);
                fail(std::string("unknown SAB tag ") + hex + " in record " +
                     std::to_string(m.recs.size()) + " (" + r.type + ") at byte " +
                     std::to_string(in.p - 1));
            }
            }
            if (push) r.t.push_back(std::move(t));
            if (end) break;
        }
        if (stop) break;
        if (!typed) fail("SAB record " + std::to_string(m.recs.size()) + " has no type name");
        split_type(r);
        m.recs.push_back(std::move(r));
    }
    index_subtypes(m);
    return m;
}

// ---------------------------------------------------------------------------------------------
// Cursor over one record's tokens; every accessor names the record on a mismatch.

class Cur {
public:
    Cur(const Model& m, int rec, std::size_t pos = 0) : m_(&m), r_(rec), p_(pos) {}
    const Rec& rec() const { return m_->recs[static_cast<std::size_t>(r_)]; }
    int rec_index() const { return r_; }
    std::size_t pos() const { return p_; }
    void seek(std::size_t p) { p_ = p; }
    bool end() const { return p_ >= rec().t.size(); }
    const Tok* peek(std::size_t ahead = 0) const {
        const std::size_t q = p_ + ahead;
        return q < rec().t.size() ? &rec().t[q] : nullptr;
    }
    bool peek_is(TK k, std::size_t ahead = 0) const {
        const Tok* t = peek(ahead);
        return t && t->k == k;
    }
    bool peek_word(std::string_view w) const {
        const Tok* t = peek();
        return t && (t->k == TK::Word || t->k == TK::Str) && t->s == w;
    }
    bool peek_number() const { return peek_is(TK::Num); }
    [[noreturn]] void error(const std::string& what) const {
        fail("record " + std::to_string(r_) + " (" + rec().type + "): " + what + " at token " +
             std::to_string(p_));
    }
    const Tok& take() {
        if (end()) error("unexpected end of record");
        return rec().t[p_++];
    }
    int ptr() {
        const Tok& t = take();
        if (t.k != TK::Ptr) error("expected pointer");
        if (t.i < -1 || t.i >= static_cast<std::int64_t>(m_->recs.size()))
            error("pointer $" + std::to_string(t.i) + " out of range");
        return static_cast<int>(t.i);
    }
    double num() {
        const Tok& t = take();
        if (t.k == TK::Num) return t.d;
        if (t.k == TK::Enum) return double(t.i);
        error("expected number");
    }
    std::int64_t integer() {
        const Tok& t = take();
        if ((t.k == TK::Num && t.integral) || t.k == TK::Enum) return t.i;
        if (t.k == TK::Num && std::floor(t.d) == t.d) return static_cast<std::int64_t>(t.d);
        error("expected integer");
    }
    gp_XYZ vec() {
        if (peek_is(TK::Vec)) {
            const Tok& t = take();
            return gp_XYZ(t.v[0], t.v[1], t.v[2]);
        }
        const double x = num(), y = num(), z = num();
        return gp_XYZ(x, y, z);
    }
    gp_XY vec2() {
        if (peek_is(TK::Vec2)) {
            const Tok& t = take();
            return gp_XY(t.v[0], t.v[1]);
        }
        const double x = num(), y = num();
        return gp_XY(x, y);
    }
    std::string word() {
        const Tok& t = take();
        if (t.k == TK::Word || t.k == TK::Str) return t.s;
        error("expected identifier");
    }
    // Enumerations are words in SAT and integers in SAB.
    std::string word_or_enum() {
        const Tok& t = take();
        if (t.k == TK::Word || t.k == TK::Str) return t.s;
        if (t.k == TK::Enum || (t.k == TK::Num && t.integral)) return "#" + std::to_string(t.i);
        if (t.k == TK::Bool) return t.b ? "#1" : "#0";
        error("expected enumeration");
    }
    // Two-valued logical: SAB Bool (true = the first / "special" spelling) or a SAT word.
    bool flag(std::initializer_list<std::string_view> trues,
              std::initializer_list<std::string_view> falses) {
        const Tok& t = take();
        if (t.k == TK::Bool) return t.b;
        if (t.k == TK::Enum || (t.k == TK::Num && t.integral)) return t.i != 0;
        if (t.k == TK::Word || t.k == TK::Str) {
            for (auto w : trues)
                if (t.s == w) return true;
            for (auto w : falses)
                if (t.s == w) return false;
        }
        std::string want;
        for (auto w : trues) want += std::string(w) + "|";
        for (auto w : falses) want += std::string(w) + "|";
        want.pop_back();
        error("expected " + want);
    }
    bool bound_next() const {
        const Tok* t = peek();
        return t && (t->k == TK::Bool ||
                     ((t->k == TK::Word) && (t->s == "I" || t->s == "F")));
    }
    // Interval end: SAT "I" (infinite) / "F <value>"; SAB false / true <value>.
    std::optional<double> bound() {
        const Tok& t = take();
        if (t.k == TK::Bool) {
            if (!t.b) return std::nullopt;
            return num();
        }
        if (t.k == TK::Word && t.s == "I") return std::nullopt;
        if (t.k == TK::Word && t.s == "F") return num();
        error("expected interval bound (I|F)");
    }
    void skip_bounds(int count) {
        for (int i = 0; i < count && bound_next(); ++i) bound();
    }
    // A sense/form qualifier after the defining data: one SAB Bool, or one SAT word that is
    // not an interval marker.
    void skip_opt_flag() {
        const Tok* t = peek();
        if (!t) return;
        if (m_->binary) {
            if (t->k == TK::Bool || t->k == TK::Enum) ++p_;
        } else if (t->k == TK::Word && t->s != "I" && t->s != "F") {
            ++p_;
        }
    }
    void skip_ints() {
        while (peek_is(TK::Num) && peek()->integral) ++p_;
    }

private:
    const Model* m_;
    int r_;
    std::size_t p_;
};

// Index one past the Close matching the Open at `open`.
std::size_t block_end(const Rec& r, std::size_t open) {
    int depth = 0;
    for (std::size_t i = open; i < r.t.size(); ++i) {
        if (r.t[i].k == TK::Open) ++depth;
        else if (r.t[i].k == TK::Close && --depth == 0) return i + 1;
    }
    fail("record (" + r.type + "): unbalanced subtype braces");
}

// ---------------------------------------------------------------------------------------------
// B-spline payloads. ACIS stores distinct knots with multiplicities whose sum is
// n_poles + degree - 1 (the outermost knot of each end is implicit); OCCT wants the full
// vector, so each end multiplicity grows by one (that knot never influences the curve).

bool is_form(const Tok* t) {
    return t && (t->k == TK::Word || t->k == TK::Str) &&
           (t->s == "nubs" || t->s == "nurbs" || t->s == "nullbs");
}

// Consume the head of an int_cur / par_cur subtype block before its bs3 data and return the
// approximation qualifier ("full" | "summary" | "none"; "" when absent):
//   SAT (ACIS):  [full|summary|none] nubs ...
//   ASM saves:   <save version int> <qualifier word or enum> nubs ...
// A SAB enum qualifier counts as "full" when the B-spline follows and as "summary" when a
// knot count follows instead.
std::string skip_int_cur_prefix(Cur& c) {
    auto is_qual_word = [&](std::size_t ahead) {
        const Tok* t = c.peek(ahead);
        return t && (t->k == TK::Word || t->k == TK::Str) &&
               (t->s == "full" || t->s == "summary" || t->s == "none");
    };
    if (c.peek_is(TK::Num) && c.peek()->integral &&
        (c.peek_is(TK::Enum, 1) || is_qual_word(1) || is_form(c.peek(1))))
        c.take();   // ASM save version
    if (is_qual_word(0)) return c.word();
    if (c.peek_is(TK::Enum)) {
        c.take();
        if (is_form(c.peek())) return "full";
        if (c.peek_is(TK::Num) && c.peek()->integral) return "summary";
        return "";
    }
    return "";
}

struct Knots {
    std::vector<double> k;
    std::vector<int> m;
    int sum = 0;
};

Knots read_knots(Cur& c, std::int64_t count) {
    if (count < 2 || count > 1000000) c.error("bad knot count " + std::to_string(count));
    Knots out;
    for (std::int64_t i = 0; i < count; ++i) {
        const double v = c.num();
        const auto mult = c.integer();
        if (mult < 1) c.error("bad knot multiplicity");
        if (!out.k.empty() && v <= out.k.back()) c.error("knots not increasing");
        out.k.push_back(v);
        out.m.push_back(static_cast<int>(mult));
        out.sum += static_cast<int>(mult);
    }
    return out;
}

void to_occt_knots(const Knots& kn, int degree, TColStd_Array1OfReal& k, TColStd_Array1OfInteger& m) {
    for (std::size_t i = 0; i < kn.k.size(); ++i) {
        k.SetValue(static_cast<int>(i) + 1, kn.k[i]);
        m.SetValue(static_cast<int>(i) + 1, kn.m[i]);
    }
    const int lo = 1, hi = static_cast<int>(kn.k.size());
    m.SetValue(lo, std::min(m.Value(lo) + 1, degree + 1));
    m.SetValue(hi, std::min(m.Value(hi) + 1, degree + 1));
}

// At the form word. Returns null for "nullbs".
Handle(Geom_BSplineCurve) read_bs3_curve(Cur& c) {
    const std::string form = c.word();
    if (form == "nullbs") return nullptr;
    if (form != "nubs" && form != "nurbs") c.error("unknown B-spline form '" + form + "'");
    const bool rational = form == "nurbs";
    const auto deg = c.integer();
    (void)c.word_or_enum();   // closure: open | closed | periodic (clamped storage either way)
    const auto nk = c.integer();
    if (deg < 1 || deg > 25) c.error("bad B-spline degree");
    const Knots kn = read_knots(c, nk);
    const int npoles = kn.sum - static_cast<int>(deg) + 1;
    if (npoles < static_cast<int>(deg) + 1) c.error("too few B-spline control points");
    TColgp_Array1OfPnt poles(1, npoles);
    TColStd_Array1OfReal weights(1, npoles);
    for (int i = 1; i <= npoles; ++i) {
        poles.SetValue(i, gp_Pnt(c.vec()));
        weights.SetValue(i, rational ? c.num() : 1.0);
    }
    TColStd_Array1OfReal k(1, static_cast<int>(kn.k.size()));
    TColStd_Array1OfInteger m(1, static_cast<int>(kn.k.size()));
    to_occt_knots(kn, static_cast<int>(deg), k, m);
    try {
        if (rational) return new Geom_BSplineCurve(poles, weights, k, m, static_cast<int>(deg));
        return new Geom_BSplineCurve(poles, k, m, static_cast<int>(deg));
    } catch (const Standard_Failure& e) {
        c.error(std::string("invalid B-spline curve: ") + e.GetMessageString());
    }
}

Handle(Geom2d_BSplineCurve) read_bs2_curve(Cur& c) {
    const std::string form = c.word();
    if (form == "nullbs") return nullptr;
    if (form != "nubs" && form != "nurbs") c.error("unknown B-spline form '" + form + "'");
    const bool rational = form == "nurbs";
    const auto deg = c.integer();
    (void)c.word_or_enum();
    const auto nk = c.integer();
    if (deg < 1 || deg > 25) c.error("bad 2D B-spline degree");
    const Knots kn = read_knots(c, nk);
    const int npoles = kn.sum - static_cast<int>(deg) + 1;
    if (npoles < static_cast<int>(deg) + 1) c.error("too few 2D B-spline control points");
    TColgp_Array1OfPnt2d poles(1, npoles);
    TColStd_Array1OfReal weights(1, npoles);
    for (int i = 1; i <= npoles; ++i) {
        poles.SetValue(i, gp_Pnt2d(c.vec2()));
        weights.SetValue(i, rational ? c.num() : 1.0);
    }
    TColStd_Array1OfReal k(1, static_cast<int>(kn.k.size()));
    TColStd_Array1OfInteger m(1, static_cast<int>(kn.k.size()));
    to_occt_knots(kn, static_cast<int>(deg), k, m);
    try {
        if (rational) return new Geom2d_BSplineCurve(poles, weights, k, m, static_cast<int>(deg));
        return new Geom2d_BSplineCurve(poles, k, m, static_cast<int>(deg));
    } catch (const Standard_Failure& e) {
        c.error(std::string("invalid 2D B-spline: ") + e.GetMessageString());
    }
}

bool is_closure_word(const Tok* t) {
    return t && (t->k == TK::Word || t->k == TK::Str) &&
           (t->s == "open" || t->s == "closed" || t->s == "periodic");
}

// Control net order: u varies fastest (row per v), as in ACIS bs3_surface saves. Rational
// surfaces carry the rational direction (both | u | v) between the degrees and the
// closures: "nurbs 3 3 both open open low none 10 2". Each pole is x y z [w] with the
// weight after the Cartesian position.
Handle(Geom_BSplineSurface) read_bs3_surface(Cur& c) {
    const std::string form = c.word();
    if (form == "nullbs") return nullptr;
    if (form != "nubs" && form != "nurbs") c.error("unknown B-spline form '" + form + "'");
    const bool rational = form == "nurbs";
    const auto du = c.integer();
    const auto dv = c.integer();
    if (rational && !is_closure_word(c.peek())) (void)c.word_or_enum();   // rational direction
    (void)c.word_or_enum();   // u closure
    (void)c.word_or_enum();   // v closure
    (void)c.word_or_enum();   // u singularity
    (void)c.word_or_enum();   // v singularity
    const auto nku = c.integer();
    const auto nkv = c.integer();
    if (du < 1 || du > 25 || dv < 1 || dv > 25) c.error("bad B-spline surface degree");
    const Knots ku = read_knots(c, nku);
    const Knots kv = read_knots(c, nkv);
    const int nu = ku.sum - static_cast<int>(du) + 1;
    const int nv = kv.sum - static_cast<int>(dv) + 1;
    if (nu < du + 1 || nv < dv + 1) c.error("too few B-spline surface control points");
    TColgp_Array2OfPnt poles(1, nu, 1, nv);
    TColStd_Array2OfReal weights(1, nu, 1, nv);
    for (int j = 1; j <= nv; ++j)
        for (int i = 1; i <= nu; ++i) {
            poles.SetValue(i, j, gp_Pnt(c.vec()));
            weights.SetValue(i, j, rational ? c.num() : 1.0);
        }
    TColStd_Array1OfReal uk(1, static_cast<int>(ku.k.size())), vk(1, static_cast<int>(kv.k.size()));
    TColStd_Array1OfInteger um(1, static_cast<int>(ku.k.size())), vm(1, static_cast<int>(kv.k.size()));
    to_occt_knots(ku, static_cast<int>(du), uk, um);
    to_occt_knots(kv, static_cast<int>(dv), vk, vm);
    try {
        if (rational)
            return new Geom_BSplineSurface(poles, weights, uk, vk, um, vm, static_cast<int>(du),
                                           static_cast<int>(dv));
        return new Geom_BSplineSurface(poles, uk, vk, um, vm, static_cast<int>(du),
                                       static_cast<int>(dv));
    } catch (const Standard_Failure& e) {
        c.error(std::string("invalid B-spline surface: ") + e.GetMessageString());
    }
}

// "nubs <int> <int> <enum>x4 <int> <int>" / "nurbs <int> <int> [<enum>] <enum>x4 <int> <int>"
// — the bs3_surface header.
bool is_bs3_surface_at(const Rec& r, std::size_t q, std::size_t end) {
    if (q + 9 > end) return false;
    auto integral = [&](std::size_t i) {
        return (r.t[i].k == TK::Num && r.t[i].integral) || r.t[i].k == TK::Enum;
    };
    auto enumish = [&](std::size_t i) {
        const TK k = r.t[i].k;
        return k == TK::Word || k == TK::Str || k == TK::Enum;
    };
    const Tok& f = r.t[q];
    if (!((f.k == TK::Word || f.k == TK::Str) && (f.s == "nubs" || f.s == "nurbs"))) return false;
    if (!(integral(q + 1) && integral(q + 2))) return false;
    std::size_t e = q + 3;
    if (f.s == "nurbs" && !is_closure_word(&r.t[e])) ++e;   // rational direction
    if (e + 6 > end) return false;
    return enumish(e) && enumish(e + 1) && enumish(e + 2) && enumish(e + 3) && integral(e + 4) &&
           integral(e + 5);
}

// ---------------------------------------------------------------------------------------------
// Geometry records.

struct SurfG {
    Handle(Geom_Surface) s;
    bool flip = false;       // OCCT normal = -(ACIS surface normal)
    std::string kind;        // "plane", "spline:rbblnsur", ...
    std::string missing;     // why `s` is null
};

struct SplBlock {
    Handle(Geom_BSplineSurface) s;
    std::string name;
    std::string missing;
};

struct IntBlock {
    Handle(Geom_BSplineCurve) c;
    std::string name;
    std::string missing;
    Handle(Geom2d_BSplineCurve) pc[2];   // pcurves on the two defining surfaces
    Handle(Geom_Surface) surf[2];        // the defining surfaces themselves
};

struct CurveG {
    Handle(Geom_Curve) c;
    std::string kind;
    std::string missing;
    Handle(Geom2d_BSplineCurve) pc[2];   // intcurve pcurves on its two defining surfaces
};

struct BodyR { int lump = -1, wire = -1, transform = -1; };
struct LumpR { int next = -1, shell = -1, body = -1; };
struct ShellR { int next = -1, face = -1, wire = -1, lump = -1; };
struct FaceR {
    int next = -1, loop = -1, shell = -1, surface = -1;
    bool reversed = false, dbl = false;
};
struct LoopR { int next = -1, coedge = -1, face = -1; };
struct CoedgeR {
    int next = -1, prev = -1, partner = -1, edge = -1, owner = -1, pcurve = -1;
    bool reversed = false;
};
struct EdgeR {
    int sv = -1, ev = -1, coedge = -1, curve = -1;
    bool reversed = false;
    bool params = false;
    double sp = 0.0, ep = 0.0;
};
struct VertexR { int edge = -1, point = -1; double tol = 0.0; };
struct WireR { int next = -1, coedge = -1, owner = -1; };

struct EdgeB {
    TopoDS_Edge e;
    bool along = true;        // TopoDS edge FORWARD direction == ACIS edge direction
    bool degenerate = false;  // no curve: collapsed edge (cone apex); omitted from wires
    bool ok = false;
};

class Reader {
public:
    Reader(Model m, AcisReadResult& out) : m_(std::move(m)), out_(out) {}
    void run();

private:
    Model m_;
    AcisReadResult& out_;
    double prec_ = 1e-6;
    double tol_match_ = 1e-4;
    double max_tol_ = 1e-3;
    BRep_Builder bb_;

    std::vector<std::optional<BodyR>> body_;
    std::vector<std::optional<LumpR>> lump_;
    std::vector<std::optional<ShellR>> shell_;
    std::vector<std::optional<FaceR>> face_;
    std::vector<std::optional<LoopR>> loop_;
    std::vector<std::optional<CoedgeR>> coedge_;
    std::vector<std::optional<EdgeR>> edge_;
    std::vector<std::optional<VertexR>> vertex_;
    std::vector<std::optional<WireR>> wire_;
    std::vector<std::vector<int>> coedges_of_edge_;

    std::unordered_map<int, SurfG> surf_cache_;
    std::unordered_map<int, CurveG> curve_cache_;
    std::unordered_map<int, Handle(Geom2d_Curve)> pcurve_cache_;
    std::unordered_map<int, SplBlock> spl_cache_;
    std::unordered_map<int, IntBlock> int_cache_;
    std::unordered_map<int, Handle(Geom2d_BSplineCurve)> par_cache_;
    std::set<int> in_progress_;
    std::unordered_map<int, TopoDS_Vertex> vtx_cache_;
    std::unordered_map<int, EdgeB> edge_cache_;
    std::set<std::pair<int, int>> pcurve_attached_;   // (edge record, face record)

    void warn(std::string w) { out_.warnings.push_back(std::move(w)); }
    std::string label(int r) const {
        return "#" + std::to_string(r) + " (" + m_.recs[static_cast<std::size_t>(r)].type + ")";
    }
    const Rec& rec(int r) const { return m_.recs[static_cast<std::size_t>(r)]; }

    // Common record head: $attrib [history id] [$pattern]. Some writers (ACIS 16 / TurboCAD
    // "1600") put a further integer before the pattern pointer of topology and geometry
    // records; nothing else can sit there, so it is skipped.
    Cur open(int r, bool pattern) const {
        Cur c(m_, r);
        c.ptr();
        if (m_.version >= 700 && c.peek_is(TK::Num)) c.integer();
        if (pattern && m_.version >= 700) {
            c.skip_ints();
            if (c.peek_is(TK::Ptr)) c.ptr();
        }
        return c;
    }
    void expect_base(int ptr, std::string_view base, int from) const {
        if (ptr < 0) return;
        if (rec(ptr).base != base)
            fail("record " + std::to_string(from) + " (" + rec(from).type + ") points at " +
                 label(ptr) + ", expected a " + std::string(base));
    }

    void parse_topology();
    gp_Pnt point(int r) const;
    TopoDS_Vertex vertex(int r);
    SurfG surface_payload(Cur& c, const std::string& kind);
    CurveG curve_payload(Cur& c, const std::string& kind);
    const SurfG& surface(int r);
    const CurveG& curve(int r);
    SplBlock spl_block(Cur& c);
    Handle(Geom_BSplineSurface) eval_rb_blend(Cur& c, std::string& why);
    IntBlock int_block(Cur& c);
    Handle(Geom2d_BSplineCurve) par_block(Cur& c);
    Handle(Geom2d_Curve) pcurve(int r);
    const EdgeB& edge(int r);
    bool edge_from_pcurve(int ei, EdgeB& out);
    void attach_pcurve(int ei, int fi, const TopoDS_Face& f, int coedge);
    TopoDS_Face face(int fi, std::map<int, int>& edge_uses, bool& complete);
    TopoDS_Shape heal(const TopoDS_Shape& s) const;
    std::optional<gp_GTrsf> transform(int r, bool& affine_only, gp_Trsf& trsf) const;
    double extent_ = 1.0;   // bounding-box diagonal of all ACIS points
};

// ---------------------------------------------------------------------------------------------
// Topology record parsing (all records up front, so back pointers can be resolved).

void Reader::parse_topology() {
    const std::size_t n = m_.recs.size();
    body_.assign(n, std::nullopt);
    lump_.assign(n, std::nullopt);
    shell_.assign(n, std::nullopt);
    face_.assign(n, std::nullopt);
    loop_.assign(n, std::nullopt);
    coedge_.assign(n, std::nullopt);
    edge_.assign(n, std::nullopt);
    vertex_.assign(n, std::nullopt);
    wire_.assign(n, std::nullopt);
    coedges_of_edge_.assign(n, {});
    for (int r = 0; r < static_cast<int>(n); ++r) {
        const std::string& b = rec(r).base;
        if (b == "body") {
            Cur c = open(r, true);
            BodyR x;
            x.lump = c.ptr();
            x.wire = c.ptr();
            x.transform = c.peek_is(TK::Ptr) ? c.ptr() : -1;
            expect_base(x.lump, "lump", r);
            expect_base(x.wire, "wire", r);
            expect_base(x.transform, "transform", r);
            body_[r] = x;
        } else if (b == "lump") {
            Cur c = open(r, true);
            LumpR x;
            x.next = c.ptr();
            x.shell = c.ptr();
            x.body = c.ptr();
            expect_base(x.shell, "shell", r);
            lump_[r] = x;
        } else if (b == "shell") {
            Cur c = open(r, true);
            std::vector<int> p;
            while (c.peek_is(TK::Ptr)) p.push_back(c.ptr());
            ShellR x;
            // >= 1.x: next subshell face [wire] lump
            if (p.size() >= 5) x = {p[0], p[2], p[3], p[4]};
            else if (p.size() == 4) x = {p[0], p[2], -1, p[3]};
            else if (p.size() == 3) x = {p[0], p[1], -1, p[2]};
            else c.error("unexpected shell layout");
            expect_base(x.face, "face", r);
            shell_[r] = x;
        } else if (b == "face") {
            Cur c = open(r, true);
            std::vector<int> p;
            while (c.peek_is(TK::Ptr)) p.push_back(c.ptr());
            FaceR x;
            // next loop shell [subshell] surface
            if (p.size() >= 5) {
                x.next = p[0]; x.loop = p[1]; x.shell = p[2]; x.surface = p[4];
            } else if (p.size() == 4) {
                x.next = p[0]; x.loop = p[1]; x.shell = p[2]; x.surface = p[3];
            } else {
                c.error("unexpected face layout");
            }
            x.reversed = c.flag({"reversed"}, {"forward"});
            if (!c.end() && (c.peek_is(TK::Bool) || c.peek_word("single") || c.peek_word("double")))
                x.dbl = c.flag({"double"}, {"single"});
            expect_base(x.loop, "loop", r);
            expect_base(x.surface, "surface", r);
            face_[r] = x;
        } else if (b == "loop") {
            Cur c = open(r, true);
            LoopR x;
            x.next = c.ptr();
            x.coedge = c.ptr();
            x.face = c.ptr();
            expect_base(x.coedge, "coedge", r);
            loop_[r] = x;
        } else if (b == "coedge") {
            Cur c = open(r, true);
            CoedgeR x;
            x.next = c.ptr();
            x.prev = c.ptr();
            x.partner = c.ptr();
            x.edge = c.ptr();
            x.reversed = c.flag({"reversed"}, {"forward"});
            x.owner = c.ptr();
            c.skip_ints();
            x.pcurve = c.peek_is(TK::Ptr) ? c.ptr() : -1;
            expect_base(x.edge, "edge", r);
            expect_base(x.pcurve, "pcurve", r);
            coedge_[r] = x;
        } else if (b == "edge") {
            Cur c = open(r, true);
            EdgeR x;
            x.sv = c.ptr();
            if (c.peek_number()) {
                x.sp = c.num();
                x.params = true;
            }
            x.ev = c.ptr();
            if (c.peek_number()) x.ep = c.num();
            else x.params = false;
            x.coedge = c.ptr();
            x.curve = c.ptr();
            if (!c.end() && !c.peek_is(TK::Str)) x.reversed = c.flag({"reversed"}, {"forward"});
            expect_base(x.sv, "vertex", r);
            expect_base(x.ev, "vertex", r);
            expect_base(x.curve, "curve", r);
            edge_[r] = x;
        } else if (b == "vertex") {
            Cur c = open(r, true);
            VertexR x;
            x.edge = c.ptr();
            c.skip_ints();
            x.point = c.ptr();
            if (c.peek_number()) x.tol = std::max(0.0, c.num());   // tolerant vertex
            expect_base(x.point, "point", r);
            vertex_[r] = x;
        } else if (b == "wire") {
            Cur c = open(r, true);
            std::vector<int> p;
            while (c.peek_is(TK::Ptr)) p.push_back(c.ptr());
            if (p.size() < 3) c.error("unexpected wire layout");
            wire_[r] = WireR{p[0], p[1], p[2]};
        }
    }
    for (int r = 0; r < static_cast<int>(n); ++r)
        if (coedge_[r] && coedge_[r]->edge >= 0) coedges_of_edge_[coedge_[r]->edge].push_back(r);
}

gp_Pnt Reader::point(int r) const {
    if (r < 0) fail("vertex without a point");
    Cur c = open(r, true);
    return gp_Pnt(c.vec());
}

TopoDS_Vertex Reader::vertex(int r) {
    auto it = vtx_cache_.find(r);
    if (it != vtx_cache_.end()) return it->second;
    if (!vertex_[r]) fail(label(r) + " is not a vertex");
    TopoDS_Vertex v;
    bb_.MakeVertex(v, point(vertex_[r]->point), std::max(prec_, vertex_[r]->tol));
    vtx_cache_[r] = v;
    return v;
}

// ---------------------------------------------------------------------------------------------
// Surfaces.

SplBlock Reader::spl_block(Cur& c) {
    const std::size_t open_at = c.pos();
    if (!c.peek_is(TK::Open)) c.error("expected '{' (spline surface subtype)");
    const std::size_t end = block_end(c.rec(), open_at);
    c.take();
    const std::string name = c.word();
    if (name == "ref") {
        const auto idx = c.integer();
        c.seek(end);
        if (idx < 0 || idx >= static_cast<std::int64_t>(m_.subtypes.size()))
            c.error("subtype ref " + std::to_string(idx) + " out of range");
        if (auto hit = spl_cache_.find(static_cast<int>(idx)); hit != spl_cache_.end())
            return hit->second;
        const auto [ri, pos] = m_.subtypes[static_cast<std::size_t>(idx)];
        Cur c2(m_, ri, pos);
        return spl_block(c2);
    }
    const int sid = m_.subtype_index.at({c.rec_index(), open_at});
    if (auto hit = spl_cache_.find(sid); hit != spl_cache_.end()) {
        c.seek(end);
        return hit->second;
    }
    if (!in_progress_.insert(sid).second) c.error("cyclic subtype reference");
    SplBlock out;
    out.name = name;
    const Rec& r = c.rec();
    // The approximating bs3_surface: first header at this block's top level. Exact splines
    // store it first; procedural subtypes (rbblnsur, offsur, sweepsur, ...) after their
    // defining data. Nested blocks (support surfaces/curves) are skipped.
    int depth = 0;
    std::optional<std::size_t> at;
    bool summary = false, nullbs = false;
    for (std::size_t q = c.pos(); q + 1 < end; ++q) {
        const Tok& t = r.t[q];
        if (t.k == TK::Open) ++depth;
        else if (t.k == TK::Close) --depth;
        if (depth != 0) continue;
        if (t.k == TK::Word || t.k == TK::Str) {
            if (t.s == "summary" || t.s == "none") summary = true;
            if (t.s == "nullbs") nullbs = true;
        }
        if (is_bs3_surface_at(r, q, end - 1)) {
            at = q;
            break;
        }
    }
    if (at) {
        Cur b(m_, c.rec_index(), *at);
        try {
            out.s = read_bs3_surface(b);
        } catch (const std::exception& e) {
            out.missing = e.what();
        }
        if (out.s.IsNull() && out.missing.empty()) out.missing = "approximation is nullbs";
    } else if (summary) {
        out.missing = "approximation stored only as a summary (no control points)";
    } else if (nullbs) {
        out.missing = "no approximating B-spline stored (nullbs)";
    } else {
        out.missing = "no approximating B-spline found";
    }
    if (out.s.IsNull() && name == "rbblnsur") {
        Cur e(m_, c.rec_index(), c.pos());
        std::string why;
        try {
            Handle(Geom_BSplineSurface) s = eval_rb_blend(e, why);
            if (!s.IsNull()) {
                out.s = s;
                out.missing.clear();
            }
        } catch (const std::exception& ex) {
            why = ex.what();
        } catch (const Standard_Failure& ex) {
            why = std::string("OCCT: ") + ex.GetMessageString();
        }
        if (out.s.IsNull()) out.missing += "; rolling-ball evaluation failed: " + why;
    }
    c.seek(end);
    in_progress_.erase(sid);
    spl_cache_[sid] = out;
    return out;
}

// Constant-radius rolling-ball blend (rb_blend_spl_sur) evaluated from its definition, for
// records that store no approximation ("summary"). Layout after the subtype name:
//   <support> <support> <defining curve> <left off> <right off> no_radius <u range> <v range> ..
//   support = blendsupsur <inline surface> <inline curve|null_curve> <bs2|nullbs> <numbers>
// The ball centre sits at the signed offsets (along each support's ACIS normal) from both
// supports, in the plane normal to the defining curve; the section is the circular arc
// between the two contact points, u = 0 on the left support, v along the defining curve —
// the parametrisation ACIS uses, so the surface normal needs no flip (checked against the
// stored approximations of Inventor/AutoCAD blends: deviation within their fit tolerance,
// normals agree).
Handle(Geom_BSplineSurface) Reader::eval_rb_blend(Cur& c, std::string& why) {
    struct Sup {
        Handle(Geom_Surface) s;
        bool flip = false;
    };
    Sup sup[2];
    for (auto& sp : sup) {
        const std::string tag = c.word();
        if (tag != "blendsupsur") {
            why = "support '" + tag + "' is not a surface";
            return nullptr;
        }
        const std::string kind = c.word();
        const SurfG g = surface_payload(c, kind);
        if (g.s.IsNull()) {
            why = "support surface [" + g.kind + "]: " + g.missing;
            return nullptr;
        }
        sp.s = g.s;
        sp.flip = g.flip;
        const std::string ck = c.word();
        if (ck != "null_curve") curve_payload(c, ck);
        if (is_form(c.peek())) read_bs2_curve(c);
        while (c.peek_number()) c.num();
    }
    const std::string dk = c.word();
    const CurveG dc = curve_payload(c, dk);
    if (dc.c.IsNull()) {
        why = "defining curve [" + dc.kind + "]: " + dc.missing;
        return nullptr;
    }
    const double off0 = c.num();
    const double off1 = c.num();
    const double off[2] = {off0, off1};
    const std::string radius = c.word();
    if (radius != "no_radius") {
        why = "variable-radius blend (" + radius + ")";
        return nullptr;
    }
    c.bound();   // u range (0..1 across the section)
    c.bound();
    const std::optional<double> v0 = c.bound(), v1 = c.bound();
    double va = v0 ? *v0 : dc.c->FirstParameter();
    double vb = v1 ? *v1 : dc.c->LastParameter();
    if (Precision::IsInfinite(va) || Precision::IsInfinite(vb) || !(vb > va)) {
        why = "unbounded blend v range";
        return nullptr;
    }
    const double eps = 1e-12 * std::max(1.0, extent_);
    // Centre P(v) and contacts Q[k]: Newton on |signed distance to support k| = off[k] and
    // (P - C(v)) . C'(v) = 0, starting from the defining curve.
    auto solve = [&](double v, gp_Pnt& P, gp_Pnt Q[2], gp_Dir N[2]) -> bool {
        gp_Pnt C;
        gp_Vec T;
        dc.c->D1(v, C, T);
        if (T.Magnitude() < 1e-300) return false;
        const gp_Dir t(T);
        P = C;
        for (int it = 0; it < 60; ++it) {
            double f[3];
            gp_XYZ row[3];
            for (int k = 0; k < 2; ++k) {
                GeomAPI_ProjectPointOnSurf pr(P, sup[k].s);
                if (pr.NbPoints() == 0) return false;
                double pu, pv;
                pr.LowerDistanceParameters(pu, pv);
                GeomLProp_SLProps pp(sup[k].s, pu, pv, 1, Precision::Confusion());
                if (!pp.IsNormalDefined()) return false;
                gp_Dir n = pp.Normal();
                if (sup[k].flip) n.Reverse();
                Q[k] = pr.NearestPoint();
                N[k] = n;
                f[k] = gp_Vec(Q[k], P).Dot(gp_Vec(n)) - off[k];
                row[k] = n.XYZ();
            }
            f[2] = gp_Vec(C, P).Dot(gp_Vec(t));
            row[2] = t.XYZ();
            if (std::max({std::abs(f[0]), std::abs(f[1]), std::abs(f[2])}) < eps) return true;
            const gp_Mat M(row[0].X(), row[0].Y(), row[0].Z(), row[1].X(), row[1].Y(), row[1].Z(),
                           row[2].X(), row[2].Y(), row[2].Z());
            if (std::abs(M.Determinant()) < 1e-12) return false;
            const gp_XYZ step = gp_XYZ(-f[0], -f[1], -f[2]).Multiplied(M.Inverted());   // M^-1 * (-f)
            P.Translate(gp_Vec(step));
            if (it == 59) return step.Modulus() < 1e3 * eps;
        }
        return false;
    };
    // Where the supports touch (e.g. equal pipes meeting) the section collapses to a point;
    // that row of the surface is degenerate, like a cone apex.
    auto arc = [](const gp_Pnt& P, const gp_Pnt Q[2], double u) {
        const gp_Vec a(P, Q[0]), b(P, Q[1]);
        const gp_Vec ax = a.Crossed(b);
        if (ax.Magnitude() <= 1e-9 * a.SquareMagnitude())
            return gp_Pnt(Q[0].XYZ() * (1.0 - u) + Q[1].XYZ() * u);
        const double th = a.Angle(b);
        gp_Vec r = a.Rotated(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(ax)), u * th);
        // Equal offsets give a circle; unequal ones blend the radius linearly.
        r.Multiply(((1.0 - u) * a.Magnitude() + u * b.Magnitude()) / a.Magnitude());
        return P.Translated(r);
    };
    Handle(Geom_BSplineSurface) best;
    for (int nv = 33, nu = 9, round = 0; round < 4; ++round, nv = 2 * nv - 1, nu = 2 * nu - 1) {
        TColgp_Array2OfPnt pts(1, nu, 1, nv);
        for (int j = 1; j <= nv; ++j) {
            const double v = va + (vb - va) * (j - 1) / (nv - 1);
            gp_Pnt P, Q[2];
            gp_Dir N[2];
            if (!solve(v, P, Q, N)) {
                why = "no ball centre at v=" + std::to_string(v);
                return nullptr;
            }
            const gp_Vec a(P, Q[0]), b(P, Q[1]);
            if (a.Crossed(b).Magnitude() <= 1e-9 * a.SquareMagnitude() && a.Dot(b) < 0.0) {
                why = "half-turn cross-section at v=" + std::to_string(v);
                return nullptr;
            }
            for (int i = 1; i <= nu; ++i) pts.SetValue(i, j, arc(P, Q, double(i - 1) / (nu - 1)));
        }
        GeomAPI_PointsToBSplineSurface fit;
        fit.Init(pts, Approx_IsoParametric, 3, 8, GeomAbs_C2, 0.01 * tol_match_);
        if (!fit.IsDone()) {
            why = "B-spline fit failed";
            return nullptr;
        }
        best = fit.Surface();
        // Deviation at the mid-samples between the fitted rows/columns.
        double dev = 0.0;
        gp_Pnt Pm, Qm[2];
        gp_Dir Nm[2];
        for (int j = 1; j < nv; j += std::max(1, (nv - 1) / 16)) {
            const double v = va + (vb - va) * (j - 0.5) / (nv - 1);
            if (!solve(v, Pm, Qm, Nm)) continue;
            for (double u : {0.0, 0.5 / (nu - 1), 0.5}) {
                const gp_Pnt x = arc(Pm, Qm, u);
                GeomAPI_ProjectPointOnSurf pr(x, best);
                if (pr.NbPoints() > 0) dev = std::max(dev, pr.LowerDistance());
            }
        }
        if (dev < 0.1 * tol_match_) break;
        if (round == 3) {
            why = "fit deviation " + std::to_string(dev) + " above tolerance";
            return nullptr;
        }
    }
    return best;
}

SurfG Reader::surface_payload(Cur& c, const std::string& kind) {
    SurfG g;
    g.kind = kind;
    if (kind == "plane") {
        const gp_XYZ o = c.vec(), nrm = c.vec(), u = c.vec();
        c.skip_opt_flag();   // reverse_v
        c.skip_bounds(4);
        if (nrm.Modulus() < 1e-300) {
            g.missing = "plane with zero normal";
            return g;
        }
        gp_Ax3 ax = (u.Crossed(nrm).Modulus() > 1e-12 * u.Modulus() && u.Modulus() > 1e-300)
                        ? gp_Ax3(gp_Pnt(o), gp_Dir(nrm), gp_Dir(u))
                        : gp_Ax3(gp_Pnt(o), gp_Dir(nrm));
        g.s = new Geom_Plane(ax);
        return g;
    }
    if (kind == "cone") {
        const gp_XYZ ctr = c.vec(), axis = c.vec(), major = c.vec();
        const double ratio = c.num();
        if (c.bound_next()) c.skip_bounds(2);
        const double sine = c.num();
        const double cosine = c.num();
        if (c.peek_number()) c.num();   // u parameter scale (>= 4.0)
        c.skip_opt_flag();
        c.skip_bounds(4);
        const double R = major.Modulus();
        if (R < 1e-300 || axis.Modulus() < 1e-300) {
            g.missing = "cone with degenerate base ellipse";
            return g;
        }
        // ACIS normal: outward (away from the axis) when cos > 0. The generator slope is
        // sin/cos regardless of the normal sense.
        g.flip = cosine < 0.0;
        const gp_Ax3 ax{gp_Pnt(ctr), gp_Dir(axis), gp_Dir(major)};
        const bool circular = std::abs(std::abs(ratio) - 1.0) < 1e-10;
        if (std::abs(sine) < 1e-12) {
            g.kind = "cylinder";
            if (circular) {
                g.s = new Geom_CylindricalSurface(ax, R);
            } else {
                const double r2 = R * std::abs(ratio);
                Handle(Geom_Ellipse) base =
                    r2 <= R ? new Geom_Ellipse(ax.Ax2(), R, r2)
                            : new Geom_Ellipse(gp_Ax2(gp_Pnt(ctr), gp_Dir(axis), ax.YDirection()), r2, R);
                g.s = new Geom_SurfaceOfLinearExtrusion(base, gp_Dir(axis));
                g.kind = "elliptic cylinder";
            }
            return g;
        }
        const double ang = std::atan2(sine * (cosine < 0.0 ? -1.0 : 1.0), std::abs(cosine));
        if (std::abs(ang) > kPi / 2.0 - 1e-9) {
            g.missing = "cone half angle out of range";
            return g;
        }
        Handle(Geom_ConicalSurface) cone = new Geom_ConicalSurface(ax, ang, R);
        if (circular) {
            g.s = cone;
            return g;
        }
        // Elliptic cone: the affine image of the circular cone (minor axis scaled by
        // `ratio`), exact as a rational B-spline over the nappe that holds the base ellipse.
        const double apex_v = -R / std::sin(ang);
        const double span = std::abs(apex_v) + 4.0 * extent_;
        const double v0 = apex_v < 0.0 ? apex_v : apex_v - span;
        const double v1 = apex_v < 0.0 ? apex_v + span : apex_v;
        Handle(Geom_BSplineSurface) bs = GeomConvert::SurfaceToBSplineSurface(
            new Geom_RectangularTrimmedSurface(cone, 0.0, 2.0 * kPi, v0, v1));
        gp_Trsf to_local, from_local;
        to_local.SetTransformation(ax);
        from_local = to_local.Inverted();
        for (int i = 1; i <= bs->NbUPoles(); ++i)
            for (int j = 1; j <= bs->NbVPoles(); ++j) {
                gp_Pnt p = bs->Pole(i, j).Transformed(to_local);
                p.SetY(p.Y() * std::abs(ratio));
                bs->SetPole(i, j, p.Transformed(from_local));
            }
        g.s = bs;
        g.kind = "elliptic cone";
        return g;
    }
    if (kind == "sphere") {
        const gp_XYZ ctr = c.vec();
        const double r = c.num();
        gp_XYZ uv(1.0, 0.0, 0.0), pole(0.0, 0.0, 1.0);
        if (c.peek_is(TK::Vec) || c.peek_number()) {
            uv = c.vec();
            pole = c.vec();
        }
        c.skip_opt_flag();
        c.skip_bounds(4);
        if (std::abs(r) < 1e-300) {
            g.missing = "sphere with zero radius";
            return g;
        }
        g.flip = r < 0.0;   // negative radius: normal points at the centre
        const gp_Ax3 ax = uv.Crossed(pole).Modulus() > 1e-12
                              ? gp_Ax3(gp_Pnt(ctr), gp_Dir(pole), gp_Dir(uv))
                              : gp_Ax3(gp_Pnt(ctr), gp_Dir(pole));
        g.s = new Geom_SphericalSurface(ax, std::abs(r));
        return g;
    }
    if (kind == "torus") {
        const gp_XYZ ctr = c.vec(), nrm = c.vec();
        const double major = c.num();
        const double minor = c.num();
        gp_XYZ uv(0.0, 0.0, 0.0);
        if (c.peek_is(TK::Vec) || c.peek_number()) uv = c.vec();
        c.skip_opt_flag();
        c.skip_bounds(4);
        if (std::abs(minor) < 1e-300 || nrm.Modulus() < 1e-300) {
            g.missing = "torus with zero minor radius";
            return g;
        }
        const gp_Ax3 ax = uv.Crossed(nrm).Modulus() > 1e-12
                              ? gp_Ax3(gp_Pnt(ctr), gp_Dir(nrm), gp_Dir(uv))
                              : gp_Ax3(gp_Pnt(ctr), gp_Dir(nrm));
        // Apple (|major| < |minor|) and lemon (major < 0) tori share the point set of the
        // spindle torus with |major|; the normal sense comes from the ACIS parametrisation
        // P(u,v) = C + (major + minor cos v) rad(u) + minor sin v N, normal = cos v rad + sin v N.
        Handle(Geom_ToroidalSurface) tor = new Geom_ToroidalSurface(ax, std::abs(major), std::abs(minor));
        g.s = tor;
        if (major < 0.0) g.kind = "torus (lemon)";
        else if (major < std::abs(minor)) g.kind = "torus (apple)";
        const double u = 0.3, v = kPi / 2.0;
        const gp_XYZ rad = ax.XDirection().XYZ() * std::cos(u) + ax.YDirection().XYZ() * std::sin(u);
        const gp_Pnt P(ctr + rad * (major + minor * std::cos(v)) + nrm.Normalized() * (minor * std::sin(v)));
        const gp_XYZ n_acis = rad * std::cos(v) + nrm.Normalized() * std::sin(v);
        GeomAPI_ProjectPointOnSurf proj(P, tor);
        if (proj.NbPoints() > 0) {
            double pu, pv;
            proj.LowerDistanceParameters(pu, pv);
            GeomLProp_SLProps props(tor, pu, pv, 1, Precision::Confusion());
            if (props.IsNormalDefined()) g.flip = props.Normal().XYZ().Dot(n_acis) < 0.0;
        } else {
            g.flip = minor < 0.0;
        }
        return g;
    }
    if (kind == "spline") {
        bool reversed = false;
        if (c.peek_number()) reversed = c.integer() != 0;
        else reversed = c.flag({"reversed"}, {"forward"});
        const SplBlock b = spl_block(c);
        c.skip_bounds(4);
        g.kind = "spline:" + b.name;
        g.s = b.s;
        g.missing = b.missing;
        g.flip = reversed;
        return g;
    }
    if (kind == "null_surface") {
        g.missing = "null surface";
        return g;
    }
    g.missing = "unsupported surface type '" + kind + "'";
    return g;
}

const SurfG& Reader::surface(int r) {
    if (auto it = surf_cache_.find(r); it != surf_cache_.end()) return it->second;
    SurfG g;
    try {
        Cur c = open(r, true);
        g = surface_payload(c, rec(r).kind);
    } catch (const std::exception& e) {
        g.kind = rec(r).kind;
        g.missing = e.what();
    } catch (const Standard_Failure& e) {
        g.kind = rec(r).kind;
        g.missing = std::string("OCCT: ") + e.GetMessageString();
    }
    return surf_cache_[r] = g;
}

// ---------------------------------------------------------------------------------------------
// Curves.

IntBlock Reader::int_block(Cur& c) {
    const std::size_t open_at = c.pos();
    if (!c.peek_is(TK::Open)) c.error("expected '{' (intcurve subtype)");
    const std::size_t end = block_end(c.rec(), open_at);
    c.take();
    const std::string name = c.word();
    if (name == "ref") {
        const auto idx = c.integer();
        c.seek(end);
        if (idx < 0 || idx >= static_cast<std::int64_t>(m_.subtypes.size()))
            c.error("subtype ref " + std::to_string(idx) + " out of range");
        if (auto hit = int_cache_.find(static_cast<int>(idx)); hit != int_cache_.end())
            return hit->second;
        const auto [ri, pos] = m_.subtypes[static_cast<std::size_t>(idx)];
        Cur c2(m_, ri, pos);
        return int_block(c2);
    }
    const int sid = m_.subtype_index.at({c.rec_index(), open_at});
    if (auto hit = int_cache_.find(sid); hit != int_cache_.end()) {
        c.seek(end);
        return hit->second;
    }
    if (!in_progress_.insert(sid).second) c.error("cyclic subtype reference");
    IntBlock out;
    out.name = name;
    // int_cur common data comes first for every subtype: [save version] [approximation
    // qualifier] bs3_curve fitol surf1 surf2 pcur1 pcur2 ... ASM (AutoCAD 2013+) writes the
    // newer spellings (exact_int_cur, ...) with a leading integer save version (e.g. 21500)
    // and the qualifier as an enum (SAB) instead of the word full|summary|none.
    const std::string qual = skip_int_cur_prefix(c);
    bool common = false;   // positioned at the surf1/surf2/pcur1/pcur2 data
    try {
        if (is_form(c.peek())) {
            out.c = read_bs3_curve(c);
            if (out.c.IsNull()) out.missing = "no approximating B-spline stored (nullbs)";
            if (c.peek_number()) c.num();   // fitol
            common = true;
        } else if (qual == "summary") {
            // "summary <n> <knots..> [fitol] <closure>": no control points. The curve is
            // still recoverable from a pcurve on one of its defining surfaces.
            out.missing = "approximation stored only as a summary (no control points)";
            c.integer();
            while (c.peek_number()) c.num();
            if (c.peek_is(TK::Word) || c.peek_is(TK::Enum)) c.take();
            common = true;
        } else {
            out.missing = qual.empty() || qual == "full"
                              ? "no approximating B-spline found"
                              : "approximation stored only as '" + qual + "' (no control points)";
        }
    } catch (const std::exception& e) {
        out.missing = e.what();
    }
    if (common) {
        // The defining surfaces and the pcurves on them (pcurve records may reference them;
        // a curve without control points is rebuilt from them). Unknown inline surface
        // data just ends the scan.
        try {
            for (auto& s : out.surf) {
                const std::string sk = c.word();
                if (sk != "null_surface") {
                    SurfG sg = surface_payload(c, sk);
                    s = sg.s;
                }
            }
            for (auto& pc : out.pc)
                if (is_form(c.peek())) pc = read_bs2_curve(c);
        } catch (const std::exception&) {
        } catch (const Standard_Failure&) {
        }
    }
    if (out.c.IsNull()) {
        for (int k = 0; k < 2 && out.c.IsNull(); ++k) {
            if (out.surf[k].IsNull() || out.pc[k].IsNull()) continue;
            try {
                TopoDS_Edge e;
                bb_.MakeEdge(e);
                bb_.UpdateEdge(e, out.pc[k], out.surf[k], TopLoc_Location(), prec_);
                bb_.Range(e, out.pc[k]->FirstParameter(), out.pc[k]->LastParameter());
                if (!BRepLib::BuildCurve3d(e, prec_)) continue;
                double f, l;
                Handle(Geom_Curve) c3 = BRep_Tool::Curve(e, f, l);
                if (c3.IsNull()) continue;
                Handle(Geom_BSplineCurve) bs = Handle(Geom_BSplineCurve)::DownCast(c3);
                if (bs.IsNull()) bs = GeomConvert::CurveToBSplineCurve(new Geom_TrimmedCurve(c3, f, l));
                out.c = bs;
            } catch (const Standard_Failure&) {
            }
        }
    }
    c.seek(end);
    in_progress_.erase(sid);
    int_cache_[sid] = out;
    return out;
}

CurveG Reader::curve_payload(Cur& c, const std::string& kind) {
    CurveG g;
    g.kind = kind;
    if (kind == "straight") {
        const gp_XYZ o = c.vec(), d = c.vec();
        c.skip_bounds(2);
        if (d.Modulus() < 1e-300) {
            g.missing = "straight with zero direction";
            return g;
        }
        g.c = new Geom_Line(gp_Pnt(o), gp_Dir(d));
        return g;
    }
    if (kind == "ellipse") {
        const gp_XYZ ctr = c.vec(), nrm = c.vec(), major = c.vec();
        const double ratio = std::abs(c.num());
        c.skip_bounds(2);
        const double R = major.Modulus();
        if (R < 1e-300 || nrm.Modulus() < 1e-300) {
            g.missing = "degenerate ellipse";
            return g;
        }
        const gp_Ax2 ax{gp_Pnt(ctr), gp_Dir(nrm), gp_Dir(major)};
        if (std::abs(ratio - 1.0) < 1e-10) {
            g.c = new Geom_Circle(ax, R);
            g.kind = "circle";
        } else if (ratio < 1.0) {
            g.c = new Geom_Ellipse(ax, R, R * ratio);
        } else {
            g.c = new Geom_Ellipse(gp_Ax2(gp_Pnt(ctr), gp_Dir(nrm), ax.YDirection()), R * ratio, R);
        }
        return g;
    }
    if (kind == "intcurve") {
        bool reversed = false;
        if (c.peek_number()) reversed = c.integer() != 0;
        else reversed = c.flag({"reversed"}, {"forward"});
        const IntBlock b = int_block(c);
        c.skip_bounds(2);
        g.kind = "intcurve:" + b.name;
        g.missing = b.missing;
        g.pc[0] = b.pc[0];
        g.pc[1] = b.pc[1];
        if (!b.c.IsNull()) {
            g.c = reversed ? Handle(Geom_Curve)(b.c->Reversed()) : Handle(Geom_Curve)(b.c);
            g.missing.clear();
        }
        return g;
    }
    if (kind == "null_curve") {
        g.missing = "null curve";
        return g;
    }
    g.missing = "unsupported curve type '" + kind + "'";
    return g;
}

const CurveG& Reader::curve(int r) {
    if (auto it = curve_cache_.find(r); it != curve_cache_.end()) return it->second;
    CurveG g;
    try {
        Cur c = open(r, true);
        g = curve_payload(c, rec(r).kind);
    } catch (const std::exception& e) {
        g.kind = rec(r).kind;
        g.missing = e.what();
    } catch (const Standard_Failure& e) {
        g.kind = rec(r).kind;
        g.missing = std::string("OCCT: ") + e.GetMessageString();
    }
    return curve_cache_[r] = g;
}

Handle(Geom2d_BSplineCurve) Reader::par_block(Cur& c) {
    const std::size_t open_at = c.pos();
    const std::size_t end = block_end(c.rec(), open_at);
    c.take();
    const std::string name = c.word();
    if (name == "ref") {
        const auto idx = c.integer();
        c.seek(end);
        if (idx < 0 || idx >= static_cast<std::int64_t>(m_.subtypes.size())) return nullptr;
        if (auto hit = par_cache_.find(static_cast<int>(idx)); hit != par_cache_.end())
            return hit->second;
        const auto [ri, pos] = m_.subtypes[static_cast<std::size_t>(idx)];
        Cur c2(m_, ri, pos);
        return par_block(c2);
    }
    const int sid = m_.subtype_index.at({c.rec_index(), open_at});
    if (auto hit = par_cache_.find(sid); hit != par_cache_.end()) {
        c.seek(end);
        return hit->second;
    }
    Handle(Geom2d_BSplineCurve) out;
    (void)skip_int_cur_prefix(c);
    if (is_form(c.peek())) {
        try {
            out = read_bs2_curve(c);
        } catch (const std::exception&) {
            out.Nullify();
        }
    }
    c.seek(end);
    par_cache_[sid] = out;
    return out;
}

// pcurve record: type 0 = own definition `sense { exppc ... } du dv`; type +-1/+-2 = the
// first/second pcurve of the referenced intcurve (negative: reversed), then `du dv`.
Handle(Geom2d_Curve) Reader::pcurve(int r) {
    if (auto it = pcurve_cache_.find(r); it != pcurve_cache_.end()) return it->second;
    Handle(Geom2d_Curve) out;
    try {
        Cur c = open(r, true);
        const auto type = c.integer();
        Handle(Geom2d_BSplineCurve) bs;
        bool reversed = false;
        if (type == 0) {
            reversed = c.flag({"reversed"}, {"forward"});
            if (c.peek_is(TK::Open)) bs = par_block(c);
        } else {
            const int ic = c.ptr();
            reversed = type < 0;
            const auto which = std::abs(type);
            if (ic >= 0 && rec(ic).base == "curve" && (which == 1 || which == 2))
                bs = curve(ic).pc[which - 1];
        }
        double du = 0.0, dv = 0.0;
        if (c.peek_number()) du = c.num();
        if (c.peek_number()) dv = c.num();
        if (!bs.IsNull()) {
            Handle(Geom2d_BSplineCurve) copy = Handle(Geom2d_BSplineCurve)::DownCast(bs->Copy());
            if (du != 0.0 || dv != 0.0) copy->Translate(gp_Vec2d(du, dv));
            if (reversed) copy->Reverse();
            out = copy;
        }
    } catch (const std::exception&) {
        out.Nullify();   // OCCT computes the pcurve instead
    }
    return pcurve_cache_[r] = out;
}

// ---------------------------------------------------------------------------------------------
// Edges.

double project_on(const Handle(Geom_Curve)& c, const gp_Pnt& p) {
    if (auto l = Handle(Geom_Line)::DownCast(c)) return ElCLib::Parameter(l->Lin(), p);
    if (auto ci = Handle(Geom_Circle)::DownCast(c)) return ElCLib::Parameter(ci->Circ(), p);
    if (auto el = Handle(Geom_Ellipse)::DownCast(c)) return ElCLib::Parameter(el->Elips(), p);
    const double f = c->FirstParameter(), l = c->LastParameter();
    double best = f, bestd = c->Value(f).SquareDistance(p);
    const double dl = c->Value(l).SquareDistance(p);
    if (dl < bestd) {
        best = l;
        bestd = dl;
    }
    GeomAPI_ProjectPointOnCurve proj(p, c, f, l);
    for (int i = 1; i <= proj.NbPoints(); ++i) {
        const double d = proj.Point(i).SquareDistance(p);
        if (d < bestd) {
            bestd = d;
            best = proj.Parameter(i);
        }
    }
    return best;
}

bool Reader::edge_from_pcurve(int ei, EdgeB& out) {
    const EdgeR& er = *edge_[ei];
    if (er.sv < 0 || er.ev < 0) return false;
    const gp_Pnt ps = point(vertex_[er.sv]->point), pe = point(vertex_[er.ev]->point);
    for (int ci : coedges_of_edge_[ei]) {
        const CoedgeR& ce = *coedge_[ci];
        if (ce.pcurve < 0 || ce.owner < 0 || !loop_[ce.owner]) continue;
        const int fi = loop_[ce.owner]->face;
        if (fi < 0 || !face_[fi]) continue;
        const SurfG& sg = surface(face_[fi]->surface);
        if (sg.s.IsNull()) continue;
        Handle(Geom2d_Curve) pc = pcurve(ci);
        if (pc.IsNull()) continue;
        const double f = pc->FirstParameter(), l = pc->LastParameter();
        const gp_Pnt pf = sg.s->Value(pc->Value(f).X(), pc->Value(f).Y());
        const gp_Pnt pl = sg.s->Value(pc->Value(l).X(), pc->Value(l).Y());
        // The pcurve runs along the coedge; the coedge runs along the edge unless reversed.
        bool along;
        if (pf.Distance(ps) < tol_match_ && pl.Distance(pe) < tol_match_) along = true;
        else if (pf.Distance(pe) < tol_match_ && pl.Distance(ps) < tol_match_) along = false;
        else continue;
        try {
            TopoDS_Edge e;
            bb_.MakeEdge(e);
            bb_.UpdateEdge(e, pc, sg.s, TopLoc_Location(), prec_);
            TopoDS_Vertex v1 = vertex(along ? er.sv : er.ev), v2 = vertex(along ? er.ev : er.sv);
            bb_.Add(e, v1.Oriented(TopAbs_FORWARD));
            bb_.Add(e, v2.Oriented(TopAbs_REVERSED));
            bb_.Range(e, f, l);
            if (!BRepLib::BuildCurve3d(e, prec_)) continue;
            out.e = e;
            out.along = along;
            out.ok = true;
            pcurve_attached_.insert({ei, fi});
            return true;
        } catch (const Standard_Failure&) {
            continue;
        }
    }
    return false;
}

const EdgeB& Reader::edge(int ei) {
    if (auto it = edge_cache_.find(ei); it != edge_cache_.end()) return it->second;
    EdgeB out;
    const EdgeR& er = *edge_[ei];
    if (er.curve < 0) {
        // ACIS collapses an edge to a point at a cone apex / sphere pole; OCCT re-creates
        // the degenerated edge from the face parametrisation (ShapeFix_Wire::FixDegenerated).
        if (er.sv == er.ev) {
            out.degenerate = true;
            out.ok = true;
        } else {
            warn("edge " + label(ei) + ": no curve between distinct vertices; edge omitted");
        }
        return edge_cache_[ei] = out;
    }
    const CurveG& cg = curve(er.curve);
    if (cg.c.IsNull()) {
        if (edge_from_pcurve(ei, out)) return edge_cache_[ei] = out;
        warn("edge " + label(ei) + ": curve " + label(er.curve) + " [" + cg.kind +
             "] cannot be built: " + cg.missing + "; no usable pcurve either");
        return edge_cache_[ei] = out;
    }
    try {
        Handle(Geom_Curve) C = cg.c;
        const bool fwd = !er.reversed;
        const bool closed_edge = er.sv == er.ev;
        if (er.sv < 0 || er.ev < 0) {
            warn("edge " + label(ei) + ": missing vertex; edge omitted");
            return edge_cache_[ei] = out;
        }
        const gp_Pnt ps = point(vertex_[er.sv]->point), pe = point(vertex_[er.ev]->point);
        const gp_Pnt plo = fwd ? ps : pe, phi = fwd ? pe : ps;
        auto matches = [&](double a, double b) {
            if (!(b > a)) return false;
            if (!C->IsPeriodic()) {
                const double eps = 1e-9 * (1.0 + std::abs(C->LastParameter() - C->FirstParameter()));
                if (a < C->FirstParameter() - eps || b > C->LastParameter() + eps) return false;
            }
            return C->Value(a).Distance(plo) < tol_match_ && C->Value(b).Distance(phi) < tol_match_;
        };
        double a = 0.0, b = 0.0;
        bool have = false;
        if (er.params) {
            // Edge parameters are in the edge's sense; a reversed edge negates them.
            const std::array<std::pair<double, double>, 2> cand = {
                fwd ? std::make_pair(er.sp, er.ep) : std::make_pair(-er.ep, -er.sp),
                fwd ? std::make_pair(-er.ep, -er.sp) : std::make_pair(er.sp, er.ep)};
            for (auto [x, y] : cand)
                if (matches(x, y) && (!closed_edge || !C->IsPeriodic() ||
                                      std::abs((y - x) - C->Period()) < 1e-9 * C->Period())) {
                    a = x;
                    b = y;
                    have = true;
                    break;
                }
        }
        if (!have) {
            a = project_on(C, plo);
            b = project_on(C, phi);
            if (C->IsPeriodic()) {
                const double T = C->Period();
                if (closed_edge) {
                    b = a + T;
                } else {
                    while (b <= a + 1e-12 * T) b += T;
                    while (b > a + T) b -= T;
                }
            } else {
                const double f = C->FirstParameter(), l = C->LastParameter();
                const double eps = 1e-9 * (1.0 + std::abs(l - f));
                const bool cclosed = C->IsClosed();
                if (closed_edge) {
                    if (!cclosed) {
                        warn("edge " + label(ei) + ": closed edge on an open curve [" + cg.kind +
                             "]; edge omitted");
                        return edge_cache_[ei] = out;
                    }
                    a = f;
                    b = l;
                } else if (b <= a) {
                    if (cclosed && std::abs(a - l) < eps) a = f;
                    if (cclosed && std::abs(b - f) < eps) b = l;
                    if (b <= a && cclosed) {
                        // The edge crosses the closure of a closed B-spline: make it periodic.
                        Handle(Geom_BSplineCurve) bs = Handle(Geom_BSplineCurve)::DownCast(C->Copy());
                        if (!bs.IsNull()) {
                            bs->SetPeriodic();
                            C = bs;
                            a = project_on(C, plo);
                            b = project_on(C, phi);
                            while (b <= a) b += C->Period();
                        }
                    }
                    if (b <= a) {
                        warn("edge " + label(ei) + ": vertices out of order on its curve [" +
                             cg.kind + "]; edge omitted");
                        return edge_cache_[ei] = out;
                    }
                }
            }
        }
        TopoDS_Edge e;
        bb_.MakeEdge(e, C, prec_);
        TopoDS_Vertex vlo = vertex(fwd ? er.sv : er.ev), vhi = vertex(fwd ? er.ev : er.sv);
        bb_.Add(e, vlo.Oriented(TopAbs_FORWARD));
        bb_.Add(e, vhi.Oriented(TopAbs_REVERSED));
        bb_.Range(e, a, b);
        const double dlo = C->Value(a).Distance(BRep_Tool::Pnt(vlo));
        const double dhi = C->Value(b).Distance(BRep_Tool::Pnt(vhi));
        if (dlo > BRep_Tool::Tolerance(vlo)) bb_.UpdateVertex(vlo, dlo * 1.01);
        if (dhi > BRep_Tool::Tolerance(vhi)) bb_.UpdateVertex(vhi, dhi * 1.01);
        out.e = e;
        out.along = fwd;
        out.ok = true;
    } catch (const Standard_Failure& f) {
        warn("edge " + label(ei) + ": OCCT failure building [" + cg.kind + "]: " +
             f.GetMessageString());
    }
    return edge_cache_[ei] = out;
}

// Attach the ACIS pcurve of `coedge` to edge `ei` on face `fi` when it agrees with the 3D
// curve (same parametrisation). Otherwise ShapeFix projects one; nothing is lost.
void Reader::attach_pcurve(int ei, int fi, const TopoDS_Face& f, int coedge) {
    if (pcurve_attached_.count({ei, fi})) return;
    const int pr = coedge_[coedge]->pcurve;
    if (pr < 0) return;
    Handle(Geom2d_Curve) pc = pcurve(pr);
    if (pc.IsNull()) return;
    const TopoDS_Edge& e = edge_cache_[ei].e;
    double a, b;
    Handle(Geom_Curve) C = BRep_Tool::Curve(e, a, b);
    if (C.IsNull()) return;
    Handle(Geom_Surface) S = BRep_Tool::Surface(f);
    auto fits = [&](const Handle(Geom2d_Curve)& p) {
        if (!p->IsPeriodic()) {
            const double eps = 1e-9 * (1.0 + std::abs(b - a));
            if (a < p->FirstParameter() - eps || b > p->LastParameter() + eps) return false;
        }
        for (int k = 0; k <= 4; ++k) {
            const double t = a + (b - a) * k / 4.0;
            const gp_Pnt2d uv = p->Value(t);
            if (S->Value(uv.X(), uv.Y()).Distance(C->Value(t)) > tol_match_) return false;
        }
        return true;
    };
    try {
        Handle(Geom2d_Curve) use;
        if (fits(pc)) use = pc;
        else if (Handle(Geom2d_Curve) rev = pc->Reversed(); fits(rev)) use = rev;
        if (use.IsNull()) return;
        bb_.UpdateEdge(e, use, f, prec_);
        pcurve_attached_.insert({ei, fi});
    } catch (const Standard_Failure&) {
    }
}

// ---------------------------------------------------------------------------------------------
// Faces, shells, lumps, bodies.

TopoDS_Face Reader::face(int fi, std::map<int, int>& edge_uses, bool& complete) {
    const FaceR& fr = *face_[fi];
    const SurfG& sg = surface(fr.surface);
    if (sg.s.IsNull()) {
        warn("face " + label(fi) + ": surface " + (fr.surface >= 0 ? label(fr.surface) : "(none)") +
             " [" + sg.kind + "] cannot be built: " + sg.missing + "; face omitted");
        complete = false;
        return {};
    }
    // ACIS loops run with the face (surface normal x face sense) on their left. The OCCT
    // face TShape is built FORWARD on the OCCT surface; when the OCCT surface normal is
    // opposite the ACIS face normal every coedge flips and the face is marked REVERSED.
    const bool rev = fr.reversed != sg.flip;
    TopoDS_Face f;
    bb_.MakeFace(f, sg.s, prec_);
    const bool planar = !Handle(Geom_Plane)::DownCast(sg.s).IsNull();
    int nloops = 0;
    std::set<int> seen_loops;
    for (int li = fr.loop; li >= 0 && seen_loops.insert(li).second; li = loop_[li]->next) {
        if (!loop_[li]) fail(label(li) + " is not a loop");
        struct Use { int ci; int edge; TopoDS_Edge e; };
        std::vector<Use> uses;
        bool loop_ok = true;
        std::set<int> seen;
        for (int ci = loop_[li]->coedge; ci >= 0 && seen.insert(ci).second; ci = coedge_[ci]->next) {
            if (!coedge_[ci]) fail(label(ci) + " is not a coedge");
            const CoedgeR& ce = *coedge_[ci];
            const EdgeB& eb = edge(ce.edge);
            if (!eb.ok) {
                loop_ok = false;
                continue;
            }
            if (eb.degenerate) continue;
            const bool same = (!ce.reversed) == eb.along;
            const bool forward = same != rev;
            uses.push_back({ci, ce.edge, forward ? eb.e : TopoDS::Edge(eb.e.Reversed())});
        }
        if (!loop_ok) {
            warn("face " + label(fi) + ": loop " + label(li) + " has unbuildable edges; face omitted");
            complete = false;
            return {};
        }
        // An edge run twice by one loop is a seam on a closed surface (OCCT gives it two
        // pcurves) but a slit on an open one (revolved sheets: disc cut from rim to centre).
        // OCCT has no slits: drop them and split what remains into closed wires.
        std::map<int, int> count;
        for (const auto& u : uses) ++count[u.edge];
        const bool closed_surface = sg.s->IsUClosed() || sg.s->IsVClosed();
        std::vector<Use> kept;
        for (const auto& u : uses)
            if (count[u.edge] < 2 || closed_surface) kept.push_back(u);
        std::vector<std::vector<Use>> wires;
        if (kept.size() == uses.size()) {
            wires.push_back(kept);
        } else {
            std::vector<Use> chain;
            TopoDS_Vertex start;
            for (const auto& u : kept) {
                if (chain.empty()) start = TopExp::FirstVertex(u.e, true);
                chain.push_back(u);
                if (TopExp::LastVertex(u.e, true).IsSame(start)) {
                    wires.push_back(std::move(chain));
                    chain.clear();
                }
            }
            if (!chain.empty()) wires.push_back(std::move(chain));
        }
        for (const auto& wu : wires) {
            TopoDS_Wire w;
            bb_.MakeWire(w);
            for (const auto& u : wu) {
                ++edge_uses[u.edge];
                bb_.Add(w, u.e);
                if (!planar && count[u.edge] < 2) attach_pcurve(u.edge, fi, f, u.ci);
            }
            bb_.Add(f, w);
            ++nloops;
        }
    }
    if (nloops == 0) {
        // Loop-less ACIS faces cover the whole (closed) surface: sphere, torus.
        try {
            BRepBuilderAPI_MakeFace mk(sg.s, prec_);
            Standard_Real u0, u1, v0, v1;
            sg.s->Bounds(u0, u1, v0, v1);
            if (Precision::IsInfinite(u0) || Precision::IsInfinite(u1) || Precision::IsInfinite(v0) ||
                Precision::IsInfinite(v1) || !mk.IsDone()) {
                warn("face " + label(fi) + ": unbounded face on [" + sg.kind + "]; face omitted");
                complete = false;
                return {};
            }
            f = mk.Face();
        } catch (const Standard_Failure& e) {
            warn("face " + label(fi) + ": natural-bounds face failed: " + e.GetMessageString());
            complete = false;
            return {};
        }
    }
    f.Orientation(rev ? TopAbs_REVERSED : TopAbs_FORWARD);
    return f;
}

TopoDS_Shape Reader::heal(const TopoDS_Shape& s) const {
    Handle(ShapeFix_Shape) fx = new ShapeFix_Shape(s);
    fx->SetPrecision(prec_);
    fx->SetMinTolerance(prec_ * 0.1);
    fx->SetMaxTolerance(max_tol_);
    fx->Perform();
    return fx->Shape();
}

// ACIS transform: rows r0 r1 r2 (images of the axes), translation t, uniform scale s and
// rotate/reflect/shear hints: p' = s * (x r0 + y r1 + z r2) + t.
std::optional<gp_GTrsf> Reader::transform(int r, bool& affine_only, gp_Trsf& trsf) const {
    Cur c = open(r, false);
    gp_XYZ row[3], t;
    for (auto& v : row) v = c.vec();
    t = c.vec();
    double s = c.peek_number() ? c.num() : 1.0;
    if (s == 0.0) s = 1.0;
    gp_Mat m(row[0].X() * s, row[1].X() * s, row[2].X() * s,
             row[0].Y() * s, row[1].Y() * s, row[2].Y() * s,
             row[0].Z() * s, row[1].Z() * s, row[2].Z() * s);
    // Similarity (rotation/reflection x uniform scale) -> gp_Trsf; anything else -> gp_GTrsf.
    const gp_Mat mt = m.Transposed();
    gp_Mat mtm = mt.Multiplied(m);
    const double k = mtm.Value(1, 1);
    bool similar = k > 0.0;
    for (int i = 1; i <= 3 && similar; ++i)
        for (int j = 1; j <= 3; ++j)
            if (std::abs(mtm.Value(i, j) - (i == j ? k : 0.0)) > 1e-9 * k) similar = false;
    if (similar) {
        affine_only = true;
        trsf.SetValues(m.Value(1, 1), m.Value(1, 2), m.Value(1, 3), t.X(),
                       m.Value(2, 1), m.Value(2, 2), m.Value(2, 3), t.Y(),
                       m.Value(3, 1), m.Value(3, 2), m.Value(3, 3), t.Z());
        return std::nullopt;
    }
    affine_only = false;
    gp_GTrsf g;
    g.SetVectorialPart(m);
    g.SetTranslationPart(t);
    return g;
}

void Reader::run() {
    parse_topology();
    // Model extent drives the matching/healing tolerances.
    double lo[3] = {1e300, 1e300, 1e300}, hi[3] = {-1e300, -1e300, -1e300};
    bool any_point = false;
    for (int r = 0; r < static_cast<int>(m_.recs.size()); ++r) {
        if (rec(r).base != "point") continue;
        try {
            const gp_Pnt p = point(r);
            const double xyz[3] = {p.X(), p.Y(), p.Z()};
            for (int k = 0; k < 3; ++k) {
                lo[k] = std::min(lo[k], xyz[k]);
                hi[k] = std::max(hi[k], xyz[k]);
            }
            any_point = true;
        } catch (const std::exception&) {
        }
    }
    extent_ = any_point ? std::sqrt((hi[0] - lo[0]) * (hi[0] - lo[0]) + (hi[1] - lo[1]) * (hi[1] - lo[1]) +
                                    (hi[2] - lo[2]) * (hi[2] - lo[2]))
                        : 1.0;
    if (extent_ <= 0.0) extent_ = 1.0;
    prec_ = std::clamp(m_.res_tol, Precision::Confusion(), 1e-3 * extent_);
    tol_match_ = std::max(100.0 * prec_, 1e-7 * extent_);
    max_tol_ = std::max(1000.0 * prec_, 1e-4 * extent_);

    const int n = static_cast<int>(m_.recs.size());
    for (int bi = 0; bi < n; ++bi) {
        if (!body_[bi]) continue;
        const BodyR& br = *body_[bi];
        std::vector<TopoDS_Shape> solids, sheets, wires;
        bool all_closed = true;
        int nlumps = 0;
        // Lumps of this body: the body's chain first, then any lump pointing back at it.
        std::vector<int> lumps;
        {
            std::set<int> seen;
            for (int li = br.lump; li >= 0 && lump_[li] && seen.insert(li).second; li = lump_[li]->next)
                lumps.push_back(li);
            for (int li = 0; li < n; ++li)
                if (lump_[li] && lump_[li]->body == bi && !seen.count(li)) lumps.push_back(li);
        }
        for (int li : lumps) {
            ++nlumps;
            std::vector<int> shells;
            for (int si = 0; si < n; ++si)
                if (shell_[si] && shell_[si]->lump == li) shells.push_back(si);
            std::map<int, int> edge_uses;
            bool complete = true, sided_single = true;
            std::vector<TopoDS_Shell> built;
            for (int si : shells) {
                TopoDS_Shell sh;
                bb_.MakeShell(sh);
                int nfaces = 0;
                for (int fi = 0; fi < n; ++fi) {
                    if (!face_[fi] || face_[fi]->shell != si) continue;
                    if (face_[fi]->dbl) sided_single = false;
                    TopoDS_Face f = face(fi, edge_uses, complete);
                    if (f.IsNull()) continue;
                    bb_.Add(sh, f);
                    ++nfaces;
                }
                if (nfaces > 0) built.push_back(sh);
                // Free edges of the shell (wire hanging off a solid/sheet).
                if (shell_[si]->wire >= 0) all_closed = false;
            }
            if (built.empty()) continue;
            bool closed = complete && sided_single;
            for (const auto& [e, uses] : edge_uses)
                if (uses != 2) closed = false;
            if (closed) {
                TopoDS_Solid so;
                bb_.MakeSolid(so);
                for (const auto& sh : built) bb_.Add(so, sh);
                TopoDS_Shape healed = heal(so);
                TopoDS_Solid hs;
                int count = 0;
                for (TopExp_Explorer ex(healed, TopAbs_SOLID); ex.More(); ex.Next(), ++count)
                    if (count == 0) hs = TopoDS::Solid(ex.Current());
                if (count == 1) {
                    BRepLib::OrientClosedSolid(hs);
                    GProp_GProps gp;
                    BRepGProp::VolumeProperties(hs, gp);
                    if (gp.Mass() < 0.0) hs.Reverse();
                    if (!BRepCheck_Analyzer(hs).IsValid())
                        warn("lump " + label(li) + ": healed solid does not pass BRepCheck");
                    solids.push_back(hs);
                    continue;
                }
                warn("lump " + label(li) + ": closed shell did not heal into one solid; kept as shell");
            }
            all_closed = false;
            if (!closed && complete && sided_single)
                warn("lump " + label(li) + ": shell is open (edges not shared by exactly two faces); "
                     "imported as a sheet");
            for (const auto& sh : built) sheets.push_back(heal(sh));
        }
        // Wire bodies and free wires: every coedge owned by a wire of this body (or its shells).
        std::vector<int> body_wires;
        for (int wi = 0; wi < n; ++wi) {
            if (!wire_[wi]) continue;
            const int own = wire_[wi]->owner;
            const bool mine = own == bi || (own >= 0 && shell_[own] && shell_[own]->lump >= 0 &&
                                            std::find(lumps.begin(), lumps.end(), shell_[own]->lump) !=
                                                lumps.end());
            if (mine || (br.wire == wi)) body_wires.push_back(wi);
        }
        if (!body_wires.empty()) {
            TopoDS_Compound wc;
            bb_.MakeCompound(wc);
            std::set<int> done;
            int added = 0;
            for (int ci = 0; ci < n; ++ci) {
                if (!coedge_[ci]) continue;
                if (std::find(body_wires.begin(), body_wires.end(), coedge_[ci]->owner) == body_wires.end())
                    continue;
                const int ei = coedge_[ci]->edge;
                if (ei < 0 || !done.insert(ei).second) continue;
                const EdgeB& eb = edge(ei);
                if (!eb.ok || eb.degenerate) continue;
                bb_.Add(wc, eb.e);
                ++added;
            }
            if (added > 0) wires.push_back(wc);
            all_closed = false;
        }
        if (solids.empty() && sheets.empty() && wires.empty()) {
            warn("body " + label(bi) + ": no geometry could be built");
            continue;
        }
        TopoDS_Shape shape;
        const std::size_t parts = solids.size() + sheets.size() + wires.size();
        if (parts == 1) {
            shape = !solids.empty() ? solids[0] : !sheets.empty() ? sheets[0] : wires[0];
        } else {
            TopoDS_Compound comp;
            bb_.MakeCompound(comp);
            for (const auto* v : {&solids, &sheets, &wires})
                for (const auto& s : *v) bb_.Add(comp, s);
            shape = comp;
        }
        if (br.transform >= 0) {
            bool affine = true;
            gp_Trsf trsf;
            try {
                auto g = transform(br.transform, affine, trsf);
                if (affine) {
                    if (trsf.Form() != gp_Identity) shape = BRepBuilderAPI_Transform(shape, trsf, true).Shape();
                } else {
                    shape = BRepBuilderAPI_GTransform(shape, *g, true).Shape();
                }
            } catch (const Standard_Failure& e) {
                warn("body " + label(bi) + ": transform " + label(br.transform) +
                     " could not be applied: " + e.GetMessageString());
            }
            // A reflection turns the solids inside out; re-orient.
            if (!solids.empty()) {
                TopoDS_Compound fixed;
                bool changed = false;
                for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next()) {
                    GProp_GProps gp;
                    BRepGProp::VolumeProperties(ex.Current(), gp);
                    if (gp.Mass() < 0.0) changed = true;
                }
                if (changed) {
                    if (shape.ShapeType() == TopAbs_SOLID) {
                        shape.Reverse();
                    } else {
                        bb_.MakeCompound(fixed);
                        for (TopoDS_Iterator it(shape); it.More(); it.Next()) {
                            TopoDS_Shape s = it.Value();
                            if (s.ShapeType() == TopAbs_SOLID) {
                                GProp_GProps gp;
                                BRepGProp::VolumeProperties(s, gp);
                                if (gp.Mass() < 0.0) s.Reverse();
                            }
                            bb_.Add(fixed, s);
                        }
                        shape = fixed;
                    }
                }
            }
        }
        AcisBody body;
        body.shape = shape;
        body.solid = all_closed && nlumps > 0 && sheets.empty() && wires.empty() && !solids.empty();
        out_.bodies.push_back(std::move(body));
    }
}

}  // namespace

AcisReadResult read_acis(std::string_view data) {
    AcisReadResult out;
    const bool binary = data.substr(0, kAcisSig.size()) == kAcisSig ||
                        data.substr(0, kAsmSig.size()) == kAsmSig;
    Model m = binary ? parse_sab(data) : parse_sat(data);
    out.mm_per_unit = m.units > 0.0 ? m.units : 0.0;
    bool any_body = false;
    for (const auto& r : m.recs)
        if (r.base == "body") any_body = true;
    if (!any_body) fail("data contains no body record");
    try {
        Reader rd(std::move(m), out);
        rd.run();
    } catch (const Standard_Failure& e) {
        fail(std::string("OCCT failure: ") + e.GetMessageString());
    }
    if (out.bodies.empty()) {
        std::string why = "no body could be built";
        for (std::size_t i = 0; i < out.warnings.size() && i < 3; ++i) why += "; " + out.warnings[i];
        fail(why);
    }
    return out;
}

std::string decrypt_dxf_sat(const std::vector<std::string>& group_lines) {
    std::string out;
    for (const std::string& line : group_lines) {
        std::string plain;
        plain.reserve(line.size());
        for (std::size_t i = 0; i < line.size(); ++i) {
            auto c = static_cast<unsigned char>(line[i]);
            if (c == '^') {
                // DXF caret escape. "^ " is a literal '^' (encrypted 'A'); "^@".."^_" are
                // control characters, which the cipher leaves alone (<= 32).
                if (i + 1 < line.size()) {
                    const auto nx = static_cast<unsigned char>(line[i + 1]);
                    ++i;
                    if (nx == ' ') {
                        plain += static_cast<char>(159 - '^');
                    } else if (nx >= 0x40 && nx <= 0x5F) {
                        plain += static_cast<char>(nx - 0x40);
                    } else {
                        plain += static_cast<char>(159 - '^');
                        plain += static_cast<char>(nx > 32 && nx < 159 ? 159 - nx : nx);
                    }
                } else {
                    plain += static_cast<char>(159 - '^');   // trailing space was trimmed
                }
                continue;
            }
            plain += static_cast<char>(c > 32 && c < 159 ? 159 - c : c);
        }
        out += plain;
        out += '\n';
    }
    return out;
}

}  // namespace chudware::doc
