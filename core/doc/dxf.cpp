// DXF ASCII writer — R12 (AC1009) for 2D/3D wireframe documents, R2000 (AC1015) when the
// document carries ACIS bodies. See dxf.h.

#include "doc/dxf.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <stdexcept>

#include "doc/acis_write.h"  // encrypt_dxf_sat
#include "doc/sheet_ink.h"   // ink_utf8_next, ink_expand_callouts

namespace chudware::doc {

namespace {

struct Out {
    std::string s;
    void tag(int code, const std::string& v) {
        s += std::to_string(code);
        s += "\r\n";
        s += v;
        s += "\r\n";
    }
    void tag(int code, long long v) { tag(code, std::to_string(v)); }
    void tag(int code, int v) { tag(code, std::to_string(v)); }
    void tag(int code, const char* v) { tag(code, std::string(v)); }
    void real(int code, double v) {
        if (!(v == v)) { tag(code, "0"); return; }
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.6f", v);
        std::string t(buf);
        while (!t.empty() && t.back() == '0') t.pop_back();
        if (!t.empty() && t.back() == '.') t.pop_back();
        if (t.empty() || t == "-0") t = "0";
        tag(code, t);
    }
    void xy(int code, const std::array<double, 2>& p, bool z = true) {
        real(code, p[0]);
        real(code + 10, p[1]);
        if (z) real(code + 20, 0.0);
    }
};

struct Ext {
    bool ok = false;
    double minx = 0, miny = 0, maxx = 0, maxy = 0;
    void add(double x, double y) {
        if (!ok) { minx = maxx = x; miny = maxy = y; ok = true; return; }
        minx = std::min(minx, x); maxx = std::max(maxx, x);
        miny = std::min(miny, y); maxy = std::max(maxy, y);
    }
};

// Unit extrusion of a non-+Z arc into `n` (true), or false for the default +Z extrusion.
// Throws naming the arc when the extrusion is not finite or zero.
bool arc_extrusion(const DxfArc& a, std::array<double, 3>& n) {
    if (dxf_normal_is_z(a.normal)) return false;
    if (!dxf_unit_normal(a.normal, n))
        throw std::runtime_error(std::string("write_dxf: ") + (a.circle ? "circle" : "arc") +
                                 " on layer '" + a.layer + "' has an invalid extrusion");
    return !dxf_normal_is_z(n);
}

Ext extents(const DxfDocument& d) {
    Ext e;
    for (const auto& pl : d.polylines)
        for (const auto& p : pl.pts) e.add(p[0], p[1]);
    for (const auto& a : d.arcs) {
        std::array<double, 3> lo, hi;
        dxf_arc_box(a, lo, hi);  // WCS; centre +- radius for +Z arcs
        e.add(lo[0], lo[1]);
        e.add(hi[0], hi[1]);
    }
    for (const auto& s : d.solids)
        for (const auto& p : s.pts) e.add(p[0], p[1]);
    for (const auto& t : d.texts) e.add(t.pos[0], t.pos[1]);
    for (const auto& l : d.lines3) {
        e.add(l.a[0], l.a[1]);
        e.add(l.b[0], l.b[1]);
    }
    for (const auto& f : d.faces3)
        for (const auto& p : f.pts) e.add(p[0], p[1]);
    if (d.has_limits) {
        e.add(d.limits[0], d.limits[1]);
        e.add(d.limits[2], d.limits[3]);
    }
    return e;
}

void header(Out& o, const DxfDocument& d) {
    const Ext e = extents(d);
    std::array<double, 4> lim = d.has_limits ? d.limits
                                             : std::array<double, 4>{e.minx, e.miny, e.maxx, e.maxy};
    o.tag(0, "SECTION");
    o.tag(2, "HEADER");
    o.tag(9, "$ACADVER");
    o.tag(1, "AC1009");
    o.tag(9, "$DWGCODEPAGE");
    o.tag(3, "ANSI_1252");
    o.tag(9, "$INSBASE");
    o.xy(10, {0.0, 0.0});
    o.tag(9, "$EXTMIN");
    o.xy(10, {e.minx, e.miny});
    o.tag(9, "$EXTMAX");
    o.xy(10, {e.maxx, e.maxy});
    o.tag(9, "$LIMMIN");
    o.xy(10, {lim[0], lim[1]}, false);
    o.tag(9, "$LIMMAX");
    o.xy(10, {lim[2], lim[3]}, false);
    o.tag(9, "$LTSCALE");
    o.real(40, 1.0);
    o.tag(9, "$TEXTSTYLE");
    o.tag(7, "STANDARD");
    o.tag(0, "ENDSEC");
}

void ltype(Out& o, const char* name, const char* desc, const std::vector<double>& pattern) {
    double total = 0.0;
    for (double v : pattern) total += std::fabs(v);
    o.tag(0, "LTYPE");
    o.tag(2, name);
    o.tag(70, 0);
    o.tag(3, desc);
    o.tag(72, 65);
    o.tag(73, static_cast<int>(pattern.size()));
    o.real(40, total);
    for (double v : pattern) o.real(49, v);
}

void tables(Out& o, const DxfDocument& d) {
    o.tag(0, "SECTION");
    o.tag(2, "TABLES");

    o.tag(0, "TABLE");
    o.tag(2, "LTYPE");
    o.tag(70, 3);
    ltype(o, "CONTINUOUS", "Solid line", {});
    ltype(o, "DASHED", "Dashed __ __ __", {kInkDash[0], -kInkDash[1]});
    ltype(o, "CENTER", "Center ____ . ____ . ____",
          {kInkChain[0], -kInkChain[1], kInkChain[2], -kInkChain[3]});
    o.tag(0, "ENDTAB");

    std::vector<DxfLayerDef> layers;
    layers.push_back({"0", 7, "CONTINUOUS"});
    for (const auto& l : d.layers)
        if (l.name != "0") layers.push_back(l);
    o.tag(0, "TABLE");
    o.tag(2, "LAYER");
    o.tag(70, static_cast<int>(layers.size()));
    for (const auto& l : layers) {
        o.tag(0, "LAYER");
        o.tag(2, l.name);
        o.tag(70, 0);
        o.tag(62, l.color);
        o.tag(6, l.ltype);
    }
    o.tag(0, "ENDTAB");

    o.tag(0, "TABLE");
    o.tag(2, "STYLE");
    o.tag(70, 1);
    o.tag(0, "STYLE");
    o.tag(2, "STANDARD");
    o.tag(70, 0);
    o.real(40, 0.0);
    o.real(41, 1.0);
    o.real(50, 0.0);
    o.tag(71, 0);
    o.real(42, 3.5);
    o.tag(3, "isocp.shx");
    o.tag(4, "");
    o.tag(0, "ENDTAB");

    o.tag(0, "ENDSEC");
}

// A triangle repeats its third corner as the fourth (the R12 convention, kept in R2000);
// the degenerate third edge is always hidden and the closing edge takes bit 3.
int face_hidden_flags(const DxfFace3& f) {
    return (f.hidden_edges & 0x3) | 0x4 | ((f.hidden_edges & 0x4) ? 0x8 : 0);
}

int entities(Out& o, const DxfDocument& d) {
    o.tag(0, "SECTION");
    o.tag(2, "ENTITIES");
    int n = 0;
    for (const auto& pl : d.polylines) {
        if (pl.pts.size() < 2) continue;
        o.tag(0, "POLYLINE");
        o.tag(8, pl.layer);
        if (!pl.ltype.empty()) o.tag(6, pl.ltype);
        o.tag(66, 1);
        o.xy(10, {0.0, 0.0});
        o.tag(70, 0);
        if (pl.width > 0.0) {
            o.real(40, pl.width);
            o.real(41, pl.width);
        }
        for (const auto& p : pl.pts) {
            o.tag(0, "VERTEX");
            o.tag(8, pl.layer);
            o.xy(10, p);
        }
        o.tag(0, "SEQEND");
        o.tag(8, pl.layer);
        ++n;
    }
    for (const auto& a : d.arcs) {
        if (a.radius <= 0.0) continue;
        o.tag(0, a.circle ? "CIRCLE" : "ARC");
        o.tag(8, a.layer);
        if (!a.ltype.empty()) o.tag(6, a.ltype);
        o.real(10, a.center[0]);
        o.real(20, a.center[1]);
        o.real(30, a.z);
        o.real(40, a.radius);
        if (!a.circle) {
            o.real(50, a.start_deg);
            o.real(51, a.end_deg);
        }
        if (std::array<double, 3> nrm; arc_extrusion(a, nrm)) {
            o.real(210, nrm[0]);
            o.real(220, nrm[1]);
            o.real(230, nrm[2]);
        }
        ++n;
    }
    for (const auto& s : d.solids) {
        o.tag(0, "SOLID");
        o.tag(8, s.layer);
        o.xy(10, s.pts[0]);
        o.xy(11, s.pts[1]);
        o.xy(12, s.pts[2]);
        o.xy(13, s.pts[2]);
        ++n;
    }
    for (const auto& t : d.texts) {
        const std::string str = dxf_text_string(t.text);
        if (str.empty() || t.height <= 0.0) continue;
        o.tag(0, "TEXT");
        o.tag(8, t.layer);
        o.xy(10, t.pos);
        o.real(40, t.height);
        o.tag(1, str);
        o.tag(7, "STANDARD");
        if (t.halign != 0) o.tag(72, t.halign);
        if (t.halign != 0 || t.valign != 0) o.xy(11, t.pos);
        if (t.valign != 0) o.tag(73, t.valign);
        ++n;
    }
    for (const auto& l : d.lines3) {
        o.tag(0, "LINE");
        o.tag(8, l.layer);
        for (int k = 0; k < 3; ++k) o.real(10 + 10 * k, l.a[k]);
        for (int k = 0; k < 3; ++k) o.real(11 + 10 * k, l.b[k]);
        ++n;
    }
    for (const auto& f : d.faces3) {
        o.tag(0, "3DFACE");
        o.tag(8, f.layer);
        static constexpr int kCorner[4] = {0, 1, 2, 2};
        for (int c = 0; c < 4; ++c)
            for (int k = 0; k < 3; ++k) o.real(10 + c + 10 * k, f.pts[kCorner[c]][k]);
        o.tag(70, face_hidden_flags(f));
        ++n;
    }
    o.tag(0, "ENDSEC");
    return n;
}

// ---------------------------------------------------------------------------------------
// DXF R2000 (AC1015), written only when the document carries ACIS bodies (R12 cannot hold
// 3DSOLID/BODY). The structure mirrors a minimal AutoCAD/ODA R2000 drawing: every table,
// record, block, entity and object has a unique hex handle (5; 105 for DIMSTYLE) and an
// owner (330); tables/records carry their AcDb subclass markers; the root dictionary holds
// ACAD_GROUP, ACAD_LAYOUT (Model / Layout1 LAYOUT objects tied both ways to the
// *Model_Space / *Paper_Space block records), ACAD_MLINESTYLE (Standard), ACAD_PLOTSETTINGS
// and ACAD_PLOTSTYLENAME (whose "Normal" placeholder every layer references through 390).
// $HANDSEED is above every handle written.
namespace r2000 {

// Fixed handles of the well-known objects; everything else is allocated from kFirstFree.
enum : unsigned {
    kBlockRecTab = 0x1, kLayerTab = 0x2, kStyleTab = 0x3, kLtypeTab = 0x5, kViewTab = 0x6,
    kUcsTab = 0x7, kVportTab = 0x8, kAppidTab = 0x9, kDimstyleTab = 0xA, kRootDict = 0xC,
    kGroupDict = 0xD, kPlotStyleDict = 0xE, kPlotStyleNormal = 0xF, kLayer0 = 0x10,
    kStyleStandard = 0x11, kAppidAcad = 0x12, kLtByBlock = 0x14, kLtByLayer = 0x15,
    kLtContinuous = 0x16, kMlineDict = 0x17, kMlineStandard = 0x18, kPlotSettingsDict = 0x19,
    kLayoutDict = 0x1A, kPsRecord = 0x1B, kPsBlock = 0x1C, kPsEndblk = 0x1D, kPsLayout = 0x1E,
    kMsRecord = 0x1F, kMsBlock = 0x20, kMsEndblk = 0x21, kMsLayout = 0x22,
    kDimStandard = 0x27, kVportActive = 0x29, kLtDashed = 0x2A, kLtCenter = 0x2B,
    kFirstFree = 0x30,
};

std::string hex(unsigned h) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%X", h);
    return buf;
}

void hnd(Out& o, int code, unsigned h) { o.tag(code, hex(h)); }

// Shortest round-trip decimal; R2000 carries full double precision (solids in inches
// cannot live with R12's 6 decimals). Non-finite values are a producer bug: named, thrown.
void num(Out& o, int code, double v) {
    if (!std::isfinite(v))
        throw std::runtime_error("write_dxf: non-finite value for DXF group " +
                                 std::to_string(code));
    if (v == 0.0) { o.tag(code, "0.0"); return; }  // also folds -0
    char buf[40];
    const auto r = std::to_chars(buf, buf + sizeof(buf), v);
    std::string t(buf, r.ptr);
    if (t.find_first_of(".e") == std::string::npos) t += ".0";
    o.tag(code, t);
}

void p3(Out& o, int code, double x, double y, double z) {
    num(o, code, x);
    num(o, code + 10, y);
    num(o, code + 20, z);
}

// Millimetres per $INSUNITS unit (codes 0..20 of DXF R2000; 0 "unitless" is drawn as mm).
double mm_per_insunit(int u) {
    static constexpr double kMm[21] = {
        1.0,     25.4,   304.8,  1609344.0, 1.0,    10.0,           1000.0,
        1.0e6,   25.4e-6, 0.0254, 914.4,    1.0e-7, 1.0e-6,         1.0e-3,
        100.0,   1.0e4,  1.0e5,  1.0e12,    1.495978707e14, 9.4607304725808e18,
        3.0856776e19};
    if (u < 0 || u > 20)
        throw std::runtime_error("write_dxf: $INSUNITS " + std::to_string(u) +
                                 " is not a DXF R2000 unit code (0..20)");
    return kMm[u];
}

// $MEASUREMENT: 0 English for the imperial unit codes, 1 metric otherwise.
int measurement(int u) {
    return (u == 1 || u == 2 || u == 3 || u == 8 || u == 9 || u == 10) ? 0 : 1;
}

struct Ext3 {
    bool ok = false;
    double lo[3] = {0, 0, 0}, hi[3] = {0, 0, 0};
    void add(double x, double y, double z) {
        const double p[3] = {x, y, z};
        for (int k = 0; k < 3; ++k) {
            lo[k] = ok ? std::min(lo[k], p[k]) : p[k];
            hi[k] = ok ? std::max(hi[k], p[k]) : p[k];
        }
        ok = true;
    }
};

bool parse_number(const std::string& tok, double& v) {
    if (tok.empty()) return false;
    char* end = nullptr;
    v = std::strtod(tok.c_str(), &end);
    return end == tok.c_str() + tok.size();
}

// View extents of an ACIS body from its SAT text: every `point` record plus the full ellipse
// of every `ellipse-curve` (circular edges bulge past their vertices), mapped through the
// body `transform`. Spline edges contribute their end vertices only, so the box can be
// slightly tight on freeform bodies; it drives $EXTMIN/$EXTMAX and the initial view only.
// Record fields are read as the numbers after the record's last `$` pointer, which holds
// for both the 4.00 and 7.00 record layouts.
void sat_extents(const std::string& sat, Ext3& e) {
    std::size_t pos = 0;
    for (int line = 0; line < 3 && pos != std::string::npos; ++line) {
        pos = sat.find('\n', pos);
        if (pos != std::string::npos) ++pos;
    }
    if (pos == std::string::npos) return;
    std::istringstream in(sat.substr(pos));
    std::vector<std::array<double, 3>> points;
    std::vector<std::array<double, 10>> ellipses;
    double m[13] = {1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1};  // rows x,y,z; t; scale
    bool have_transform = false;
    std::string tok, type;
    std::vector<double> nums;
    bool fresh = true;
    while (in >> tok) {
        if (fresh) {
            double idx = 0;
            if (tok[0] == '-' && parse_number(tok, idx)) continue;  // optional "-N" index
            if (tok == "End-of-ACIS-data") break;
            type = tok;
            nums.clear();
            fresh = false;
            continue;
        }
        if (tok == "#") {
            if (type == "point" && nums.size() >= 3) {
                const std::size_t b = nums.size() - 3;
                points.push_back({nums[b], nums[b + 1], nums[b + 2]});
            } else if (type == "ellipse-curve" && nums.size() >= 10) {
                std::array<double, 10> el{};
                std::copy_n(nums.begin(), 10, el.begin());
                ellipses.push_back(el);
            } else if (type == "transform" && !have_transform && nums.size() >= 13) {
                std::copy_n(nums.begin(), 13, m);
                have_transform = true;
            }
            fresh = true;
            continue;
        }
        double v = 0;
        if (tok[0] == '$') nums.clear();
        else if (parse_number(tok, v)) nums.push_back(v);
    }
    const double s = m[12];
    auto vec = [&](double x, double y, double z) {
        return std::array<double, 3>{(x * m[0] + y * m[3] + z * m[6]) * s,
                                     (x * m[1] + y * m[4] + z * m[7]) * s,
                                     (x * m[2] + y * m[5] + z * m[8]) * s};
    };
    auto pnt = [&](double x, double y, double z) {
        auto p = vec(x, y, z);
        return std::array<double, 3>{p[0] + m[9], p[1] + m[10], p[2] + m[11]};
    };
    for (const auto& p : points) {
        const auto q = pnt(p[0], p[1], p[2]);
        e.add(q[0], q[1], q[2]);
    }
    for (const auto& el : ellipses) {
        const auto c = pnt(el[0], el[1], el[2]);
        const auto n = vec(el[3], el[4], el[5]);
        const auto maj = vec(el[6], el[7], el[8]);
        // minor semi-axis = ratio * |major| along n x major
        std::array<double, 3> mn{n[1] * maj[2] - n[2] * maj[1], n[2] * maj[0] - n[0] * maj[2],
                                 n[0] * maj[1] - n[1] * maj[0]};
        const double len = std::sqrt(mn[0] * mn[0] + mn[1] * mn[1] + mn[2] * mn[2]);
        const double majlen = std::sqrt(maj[0] * maj[0] + maj[1] * maj[1] + maj[2] * maj[2]);
        for (auto& v : mn) v = len > 0.0 ? v / len * el[9] * majlen : 0.0;
        double r[3];
        for (int k = 0; k < 3; ++k) r[k] = std::sqrt(maj[k] * maj[k] + mn[k] * mn[k]);
        e.add(c[0] - r[0], c[1] - r[1], c[2] - r[2]);
        e.add(c[0] + r[0], c[1] + r[1], c[2] + r[2]);
    }
}

// R2000 symbol names may not be empty nor contain < > / \ " : ; ? * | = `.
void check_name(const std::string& name, const char* what) {
    if (name.empty() || name.find_first_of("<>/\\\":;?*|=`") != std::string::npos)
        throw std::runtime_error(std::string("write_dxf: invalid DXF R2000 ") + what +
                                 " name '" + name + "'");
}

// Entity layer: an empty name is the default layer "0".
const std::string& layer_of(const std::string& l) {
    static const std::string kZero = "0";
    return l.empty() ? kZero : l;
}

// Document linetype names (R12 spelling, case-insensitive) -> R2000 LTYPE record names.
std::string ltype_name(const std::string& lt, const std::string& where) {
    std::string u = lt;
    for (auto& c : u) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    if (u == "CONTINUOUS") return "Continuous";
    if (u == "BYLAYER") return "ByLayer";
    if (u == "BYBLOCK") return "ByBlock";
    if (u == "DASHED" || u == "CENTER") return u;
    throw std::runtime_error("write_dxf: linetype '" + lt + "' on " + where +
                             " is not defined (DXF R2000 output defines Continuous, DASHED, "
                             "CENTER)");
}

struct Writer {
    const DxfDocument& d;
    double unit_mm = 1.0;  // mm per document unit
    unsigned next = kFirstFree;
    Ext3 ext;
    std::array<double, 4> lim{0, 0, 0, 0};
    Out classes, tables, blocks, ents, objs;
    int count = 0;

    explicit Writer(const DxfDocument& doc) : d(doc), unit_mm(mm_per_insunit(doc.insunits)) {}

    unsigned alloc() { return next++; }

    void compute_extents() {
        for (const auto& pl : d.polylines)
            for (const auto& p : pl.pts) ext.add(p[0], p[1], 0.0);
        for (const auto& a : d.arcs) {
            if (a.radius <= 0.0) continue;
            std::array<double, 3> lo, hi;
            dxf_arc_box(a, lo, hi);  // WCS position of OCS arcs
            ext.add(lo[0], lo[1], lo[2]);
            ext.add(hi[0], hi[1], hi[2]);
        }
        for (const auto& s : d.solids)
            for (const auto& p : s.pts) ext.add(p[0], p[1], 0.0);
        for (const auto& t : d.texts) ext.add(t.pos[0], t.pos[1], 0.0);
        for (const auto& l : d.lines3) {
            ext.add(l.a[0], l.a[1], l.a[2]);
            ext.add(l.b[0], l.b[1], l.b[2]);
        }
        for (const auto& f : d.faces3)
            for (const auto& p : f.pts) ext.add(p[0], p[1], p[2]);
        for (const auto& a : d.acis) sat_extents(a.sat, ext);
        if (d.has_limits) lim = d.limits;
        else if (ext.ok) lim = {ext.lo[0], ext.lo[1], ext.hi[0], ext.hi[1]};
    }

    void header(Out& o) const {
        o.tag(0, "SECTION");
        o.tag(2, "HEADER");
        o.tag(9, "$ACADVER");
        o.tag(1, "AC1015");
        o.tag(9, "$ACADMAINTVER");
        o.tag(70, 6);
        o.tag(9, "$DWGCODEPAGE");
        o.tag(3, "ANSI_1252");
        o.tag(9, "$INSBASE");
        p3(o, 10, 0.0, 0.0, 0.0);
        o.tag(9, "$EXTMIN");
        p3(o, 10, ext.lo[0], ext.lo[1], ext.lo[2]);
        o.tag(9, "$EXTMAX");
        p3(o, 10, ext.hi[0], ext.hi[1], ext.hi[2]);
        o.tag(9, "$LIMMIN");
        num(o, 10, lim[0]);
        num(o, 20, lim[1]);
        o.tag(9, "$LIMMAX");
        num(o, 10, lim[2]);
        num(o, 20, lim[3]);
        o.tag(9, "$LTSCALE");
        num(o, 40, 1.0);
        o.tag(9, "$TEXTSIZE");
        num(o, 40, 3.5 / unit_mm);
        o.tag(9, "$TEXTSTYLE");
        o.tag(7, "Standard");
        o.tag(9, "$CLAYER");
        o.tag(8, "0");
        o.tag(9, "$CELTYPE");
        o.tag(6, "ByLayer");
        o.tag(9, "$CECOLOR");
        o.tag(62, 256);
        o.tag(9, "$DIMSTYLE");
        o.tag(2, "Standard");
        o.tag(9, "$INSUNITS");
        o.tag(70, d.insunits);
        o.tag(9, "$MEASUREMENT");
        o.tag(70, measurement(d.insunits));
        o.tag(9, "$HANDSEED");
        hnd(o, 5, next);
        o.tag(0, "ENDSEC");
    }

    void write_classes() {
        Out& o = classes;
        o.tag(0, "SECTION");
        o.tag(2, "CLASSES");
        static constexpr const char* kClasses[][2] = {
            {"ACDBDICTIONARYWDFLT", "AcDbDictionaryWithDefault"},
            {"ACDBPLACEHOLDER", "AcDbPlaceHolder"},
            {"LAYOUT", "AcDbLayout"},
        };
        for (const auto& c : kClasses) {
            o.tag(0, "CLASS");
            o.tag(1, c[0]);
            o.tag(2, c[1]);
            o.tag(3, "ObjectDBX Classes");
            o.tag(90, 0);
            o.tag(280, 0);
            o.tag(281, 0);
        }
        o.tag(0, "ENDSEC");
    }

    static void table_head(Out& o, const char* name, unsigned h, int n) {
        o.tag(0, "TABLE");
        o.tag(2, name);
        hnd(o, 5, h);
        hnd(o, 330, 0);
        o.tag(100, "AcDbSymbolTable");
        o.tag(70, n);
    }

    static void record_head(Out& o, const char* type, const char* subclass, unsigned h,
                            unsigned table, int handle_code = 5) {
        o.tag(0, type);
        hnd(o, handle_code, h);
        hnd(o, 330, table);
        o.tag(100, "AcDbSymbolTableRecord");
        o.tag(100, subclass);
    }

    // Dash patterns are defined in mm (sheet_ink.h) and stored in document units.
    void ltype(const char* name, unsigned h, const char* desc,
               const std::vector<double>& pattern_mm) {
        Out& o = tables;
        record_head(o, "LTYPE", "AcDbLinetypeTableRecord", h, kLtypeTab);
        o.tag(2, name);
        o.tag(70, 0);
        o.tag(3, desc);
        o.tag(72, 65);
        o.tag(73, static_cast<int>(pattern_mm.size()));
        double total = 0.0;
        for (double v : pattern_mm) total += std::fabs(v);
        num(o, 40, total / unit_mm);
        for (double v : pattern_mm) {
            num(o, 49, v / unit_mm);
            o.tag(74, 0);
        }
    }

    void write_tables(const std::vector<DxfLayerDef>& layers) {
        Out& o = tables;
        o.tag(0, "SECTION");
        o.tag(2, "TABLES");

        // VPORT *Active: plan view framing the drawing extents.
        table_head(o, "VPORT", kVportTab, 1);
        record_head(o, "VPORT", "AcDbViewportTableRecord", kVportActive, kVportTab);
        o.tag(2, "*Active");
        o.tag(70, 0);
        const double grid = (measurement(d.insunits) == 0 ? 12.7 : 10.0) / unit_mm;
        const double cx = ext.ok ? 0.5 * (ext.lo[0] + ext.hi[0]) : 0.5 * (lim[0] + lim[2]);
        const double cy = ext.ok ? 0.5 * (ext.lo[1] + ext.hi[1]) : 0.5 * (lim[1] + lim[3]);
        constexpr double kAspect = 1.6;
        double h = ext.ok ? std::max(ext.hi[1] - ext.lo[1], (ext.hi[0] - ext.lo[0]) / kAspect)
                          : 0.0;
        h = h > 0.0 ? h * 1.1 : 10.0 * grid;
        num(o, 10, 0.0);
        num(o, 20, 0.0);
        num(o, 11, 1.0);
        num(o, 21, 1.0);
        num(o, 12, cx);
        num(o, 22, cy);
        num(o, 13, 0.0);
        num(o, 23, 0.0);
        num(o, 14, grid);
        num(o, 24, grid);
        num(o, 15, grid);
        num(o, 25, grid);
        p3(o, 16, 0.0, 0.0, 1.0);
        p3(o, 17, 0.0, 0.0, 0.0);
        num(o, 40, h);
        num(o, 41, kAspect);
        num(o, 42, 50.0);
        num(o, 43, 0.0);
        num(o, 44, 0.0);
        num(o, 50, 0.0);
        num(o, 51, 0.0);
        o.tag(71, 0);
        o.tag(72, 1000);
        o.tag(73, 1);
        o.tag(74, 3);
        o.tag(75, 0);
        o.tag(76, 0);
        o.tag(77, 0);
        o.tag(78, 0);
        o.tag(281, 0);
        o.tag(65, 1);
        p3(o, 110, 0.0, 0.0, 0.0);
        p3(o, 111, 1.0, 0.0, 0.0);
        p3(o, 112, 0.0, 1.0, 0.0);
        o.tag(79, 0);
        num(o, 146, 0.0);
        o.tag(0, "ENDTAB");

        table_head(o, "LTYPE", kLtypeTab, 5);
        ltype("ByBlock", kLtByBlock, "", {});
        ltype("ByLayer", kLtByLayer, "", {});
        ltype("Continuous", kLtContinuous, "Solid line", {});
        ltype("DASHED", kLtDashed, "Dashed __ __ __", {kInkDash[0], -kInkDash[1]});
        ltype("CENTER", kLtCenter, "Center ____ . ____ . ____",
              {kInkChain[0], -kInkChain[1], kInkChain[2], -kInkChain[3]});
        o.tag(0, "ENDTAB");

        table_head(o, "LAYER", kLayerTab, static_cast<int>(layers.size()));
        for (std::size_t i = 0; i < layers.size(); ++i) {
            const auto& l = layers[i];
            record_head(o, "LAYER", "AcDbLayerTableRecord", i == 0 ? kLayer0 : alloc(),
                        kLayerTab);
            o.tag(2, l.name);
            o.tag(70, 0);
            o.tag(62, l.color);
            o.tag(6, ltype_name(l.ltype, "layer '" + l.name + "'"));
            o.tag(370, -3);
            hnd(o, 390, kPlotStyleNormal);
        }
        o.tag(0, "ENDTAB");

        table_head(o, "STYLE", kStyleTab, 1);
        record_head(o, "STYLE", "AcDbTextStyleTableRecord", kStyleStandard, kStyleTab);
        o.tag(2, "Standard");
        o.tag(70, 0);
        num(o, 40, 0.0);
        num(o, 41, 1.0);
        num(o, 50, 0.0);
        o.tag(71, 0);
        num(o, 42, 3.5 / unit_mm);
        o.tag(3, "isocp.shx");
        o.tag(4, "");
        o.tag(0, "ENDTAB");

        table_head(o, "VIEW", kViewTab, 0);
        o.tag(0, "ENDTAB");
        table_head(o, "UCS", kUcsTab, 0);
        o.tag(0, "ENDTAB");

        table_head(o, "APPID", kAppidTab, 1);
        record_head(o, "APPID", "AcDbRegAppTableRecord", kAppidAcad, kAppidTab);
        o.tag(2, "ACAD");
        o.tag(70, 0);
        o.tag(0, "ENDTAB");

        table_head(o, "DIMSTYLE", kDimstyleTab, 1);
        o.tag(100, "AcDbDimStyleTable");
        record_head(o, "DIMSTYLE", "AcDbDimStyleTableRecord", kDimStandard, kDimstyleTab, 105);
        o.tag(2, "Standard");
        o.tag(70, 0);
        hnd(o, 340, kStyleStandard);
        o.tag(0, "ENDTAB");

        table_head(o, "BLOCK_RECORD", kBlockRecTab, 2);
        record_head(o, "BLOCK_RECORD", "AcDbBlockTableRecord", kMsRecord, kBlockRecTab);
        o.tag(2, "*Model_Space");
        hnd(o, 340, kMsLayout);
        record_head(o, "BLOCK_RECORD", "AcDbBlockTableRecord", kPsRecord, kBlockRecTab);
        o.tag(2, "*Paper_Space");
        hnd(o, 340, kPsLayout);
        o.tag(0, "ENDTAB");

        o.tag(0, "ENDSEC");
    }

    void write_blocks() {
        Out& o = blocks;
        o.tag(0, "SECTION");
        o.tag(2, "BLOCKS");
        struct B {
            unsigned rec, begin, end;
            const char* name;
            bool paper;
        };
        for (const B& b : {B{kMsRecord, kMsBlock, kMsEndblk, "*Model_Space", false},
                           B{kPsRecord, kPsBlock, kPsEndblk, "*Paper_Space", true}}) {
            o.tag(0, "BLOCK");
            hnd(o, 5, b.begin);
            hnd(o, 330, b.rec);
            o.tag(100, "AcDbEntity");
            if (b.paper) o.tag(67, 1);
            o.tag(8, "0");
            o.tag(100, "AcDbBlockBegin");
            o.tag(2, b.name);
            o.tag(70, 0);
            p3(o, 10, 0.0, 0.0, 0.0);
            o.tag(3, b.name);
            o.tag(1, "");
            o.tag(0, "ENDBLK");
            hnd(o, 5, b.end);
            hnd(o, 330, b.rec);
            o.tag(100, "AcDbEntity");
            if (b.paper) o.tag(67, 1);
            o.tag(8, "0");
            o.tag(100, "AcDbBlockEnd");
        }
        o.tag(0, "ENDSEC");
    }

    // Common model-space entity head: type, handle, owner, AcDbEntity, layer, linetype.
    void entity(const char* type, const std::string& layer, const std::string& ltype) {
        Out& o = ents;
        o.tag(0, type);
        hnd(o, 5, alloc());
        hnd(o, 330, kMsRecord);
        o.tag(100, "AcDbEntity");
        o.tag(8, layer_of(layer));
        if (!ltype.empty())
            o.tag(6, ltype_name(ltype, std::string(type) + " on layer '" + layer_of(layer) + "'"));
        ++count;
    }

    void write_entities() {
        Out& o = ents;
        o.tag(0, "SECTION");
        o.tag(2, "ENTITIES");
        static constexpr int kCorner[4] = {0, 1, 2, 2};  // triangle -> 4-corner entity
        for (const auto& pl : d.polylines) {
            if (pl.pts.size() < 2) continue;
            entity("LWPOLYLINE", pl.layer, pl.ltype);
            o.tag(100, "AcDbPolyline");
            o.tag(90, static_cast<int>(pl.pts.size()));
            o.tag(70, 0);
            if (pl.width > 0.0) num(o, 43, pl.width);
            for (const auto& p : pl.pts) {
                num(o, 10, p[0]);
                num(o, 20, p[1]);
            }
        }
        for (const auto& a : d.arcs) {
            if (a.radius <= 0.0) continue;
            entity(a.circle ? "CIRCLE" : "ARC", a.layer, a.ltype);
            o.tag(100, "AcDbCircle");
            p3(o, 10, a.center[0], a.center[1], a.z);
            num(o, 40, a.radius);
            if (std::array<double, 3> nrm; arc_extrusion(a, nrm)) p3(o, 210, nrm[0], nrm[1], nrm[2]);
            if (!a.circle) {
                o.tag(100, "AcDbArc");
                num(o, 50, a.start_deg);
                num(o, 51, a.end_deg);
            }
        }
        for (const auto& s : d.solids) {
            entity("SOLID", s.layer, "");
            o.tag(100, "AcDbTrace");
            for (int c = 0; c < 4; ++c)
                p3(o, 10 + c, s.pts[kCorner[c]][0], s.pts[kCorner[c]][1], 0.0);
        }
        for (const auto& t : d.texts) {
            const std::string str = dxf_text_string(t.text);
            if (str.empty() || t.height <= 0.0) continue;
            entity("TEXT", t.layer, "");
            o.tag(100, "AcDbText");
            p3(o, 10, t.pos[0], t.pos[1], 0.0);
            num(o, 40, t.height);
            o.tag(1, str);
            o.tag(7, "Standard");
            if (t.halign != 0) o.tag(72, t.halign);
            if (t.halign != 0 || t.valign != 0) p3(o, 11, t.pos[0], t.pos[1], 0.0);
            o.tag(100, "AcDbText");
            if (t.valign != 0) o.tag(73, t.valign);
        }
        for (const auto& l : d.lines3) {
            entity("LINE", l.layer, "");
            o.tag(100, "AcDbLine");
            p3(o, 10, l.a[0], l.a[1], l.a[2]);
            p3(o, 11, l.b[0], l.b[1], l.b[2]);
        }
        for (const auto& f : d.faces3) {
            entity("3DFACE", f.layer, "");
            o.tag(100, "AcDbFace");
            for (int c = 0; c < 4; ++c)
                p3(o, 10 + c, f.pts[kCorner[c]][0], f.pts[kCorner[c]][1], f.pts[kCorner[c]][2]);
            o.tag(70, face_hidden_flags(f));
        }
        for (std::size_t i = 0; i < d.acis.size(); ++i) {
            const auto& a = d.acis[i];
            const std::string what = std::string(a.solid ? "3DSOLID" : "BODY") + " #" +
                                     std::to_string(i) + " on layer '" + layer_of(a.layer) + "'";
            int version = 0;
            const auto r = std::from_chars(a.sat.data(), a.sat.data() + a.sat.size(), version);
            if (a.sat.empty() || r.ec != std::errc())
                throw std::runtime_error("write_dxf: " + what + " has no SAT header");
            if (version != a.acis_version)
                throw std::runtime_error("write_dxf: " + what + " declares ACIS version " +
                                         std::to_string(a.acis_version) +
                                         " but its SAT header says " + std::to_string(version));
            if (version > 400)
                throw std::runtime_error("write_dxf: " + what + " is SAT version " +
                                         std::to_string(version) +
                                         "; DXF R2000 (AC1015) embeds ACIS <= 400");
            if (a.sat.find("End-of-ACIS-data") == std::string::npos)
                throw std::runtime_error("write_dxf: " + what +
                                         " SAT text lacks the End-of-ACIS-data terminator");
            entity(a.solid ? "3DSOLID" : "BODY", a.layer, "");
            o.tag(100, "AcDbModelerGeometry");
            o.tag(70, 1);
            for (const auto& [code, value] : encrypt_dxf_sat(a.sat)) o.tag(code, value);
        }
        o.tag(0, "ENDSEC");
    }

    static void owned_by(Out& o, unsigned owner) {
        o.tag(102, "{ACAD_REACTORS");
        hnd(o, 330, owner);
        o.tag(102, "}");
        hnd(o, 330, owner);
    }

    // `owner` 0 = the root dictionary (owned by nobody, no reactors).
    void dictionary(unsigned h, unsigned owner,
                    const std::vector<std::pair<const char*, unsigned>>& entries) {
        Out& o = objs;
        o.tag(0, "DICTIONARY");
        hnd(o, 5, h);
        if (owner) owned_by(o, owner);
        else hnd(o, 330, 0);
        o.tag(100, "AcDbDictionary");
        o.tag(281, 1);
        for (const auto& [name, target] : entries) {
            o.tag(3, name);
            hnd(o, 350, target);
        }
    }

    void layout(unsigned h, unsigned record, const char* name, int tab, bool model) {
        Out& o = objs;
        o.tag(0, "LAYOUT");
        hnd(o, 5, h);
        owned_by(o, kLayoutDict);
        o.tag(100, "AcDbPlotSettings");
        o.tag(1, "");
        o.tag(2, "none_device");
        o.tag(4, "");
        o.tag(6, "");
        for (int c = 40; c <= 49; ++c) num(o, c, 0.0);
        num(o, 140, 0.0);
        num(o, 141, 0.0);
        num(o, 142, 1.0);
        num(o, 143, 1.0);
        o.tag(70, model ? 1712 : 688);
        o.tag(72, measurement(d.insunits) == 0 ? 0 : 1);
        o.tag(73, 0);
        o.tag(74, 5);
        o.tag(7, "");
        o.tag(75, 16);
        num(o, 147, 1.0);
        num(o, 148, 0.0);
        num(o, 149, 0.0);
        o.tag(100, "AcDbLayout");
        o.tag(1, name);
        o.tag(70, 1);
        o.tag(71, tab);
        num(o, 10, lim[0]);
        num(o, 20, lim[1]);
        num(o, 11, lim[2]);
        num(o, 21, lim[3]);
        p3(o, 12, 0.0, 0.0, 0.0);
        if (model && ext.ok) {
            p3(o, 14, ext.lo[0], ext.lo[1], ext.lo[2]);
            p3(o, 15, ext.hi[0], ext.hi[1], ext.hi[2]);
        } else {
            p3(o, 14, 0.0, 0.0, 0.0);
            p3(o, 15, 0.0, 0.0, 0.0);
        }
        num(o, 146, 0.0);
        p3(o, 13, 0.0, 0.0, 0.0);
        p3(o, 16, 1.0, 0.0, 0.0);
        p3(o, 17, 0.0, 1.0, 0.0);
        o.tag(76, 0);
        hnd(o, 330, record);
        if (model) hnd(o, 331, kVportActive);
    }

    void write_objects() {
        Out& o = objs;
        o.tag(0, "SECTION");
        o.tag(2, "OBJECTS");
        dictionary(kRootDict, 0,
                   {{"ACAD_GROUP", kGroupDict},
                    {"ACAD_LAYOUT", kLayoutDict},
                    {"ACAD_MLINESTYLE", kMlineDict},
                    {"ACAD_PLOTSETTINGS", kPlotSettingsDict},
                    {"ACAD_PLOTSTYLENAME", kPlotStyleDict}});
        dictionary(kGroupDict, kRootDict, {});
        dictionary(kLayoutDict, kRootDict, {{"Layout1", kPsLayout}, {"Model", kMsLayout}});
        dictionary(kMlineDict, kRootDict, {{"Standard", kMlineStandard}});
        dictionary(kPlotSettingsDict, kRootDict, {});

        o.tag(0, "ACDBDICTIONARYWDFLT");
        hnd(o, 5, kPlotStyleDict);
        owned_by(o, kRootDict);
        o.tag(100, "AcDbDictionary");
        o.tag(281, 1);
        o.tag(3, "Normal");
        hnd(o, 350, kPlotStyleNormal);
        o.tag(100, "AcDbDictionaryWithDefault");
        hnd(o, 340, kPlotStyleNormal);

        o.tag(0, "ACDBPLACEHOLDER");
        hnd(o, 5, kPlotStyleNormal);
        owned_by(o, kPlotStyleDict);

        o.tag(0, "MLINESTYLE");
        hnd(o, 5, kMlineStandard);
        owned_by(o, kMlineDict);
        o.tag(100, "AcDbMlineStyle");
        o.tag(2, "Standard");
        o.tag(70, 0);
        o.tag(3, "");
        o.tag(62, 256);
        num(o, 51, 90.0);
        num(o, 52, 90.0);
        o.tag(71, 2);
        for (double off : {0.5, -0.5}) {
            num(o, 49, off);
            o.tag(62, 256);
            o.tag(6, "BYLAYER");
        }

        layout(kMsLayout, kMsRecord, "Model", 0, true);
        layout(kPsLayout, kPsRecord, "Layout1", 1, false);
        o.tag(0, "ENDSEC");
    }

    // Layer "0", the declared layers in order, then any layer an entity references without
    // declaring it (white, Continuous) so no entity points at a missing LAYER record.
    std::vector<DxfLayerDef> layer_table() const {
        std::vector<DxfLayerDef> layers;
        std::set<std::string> seen;
        auto add = [&](const DxfLayerDef& l) {
            check_name(l.name, "layer");
            if (seen.insert(l.name).second) layers.push_back(l);
        };
        add({"0", 7, "CONTINUOUS"});
        for (const auto& l : d.layers) add(l);
        auto ref = [&](const std::string& name) {
            const std::string& n = layer_of(name);
            if (!seen.count(n)) add({n, 7, "CONTINUOUS"});
        };
        for (const auto& e : d.polylines) ref(e.layer);
        for (const auto& e : d.arcs) ref(e.layer);
        for (const auto& e : d.solids) ref(e.layer);
        for (const auto& e : d.texts) ref(e.layer);
        for (const auto& e : d.lines3) ref(e.layer);
        for (const auto& e : d.faces3) ref(e.layer);
        for (const auto& e : d.acis) ref(e.layer);
        return layers;
    }

    std::string build(int* entity_count) {
        compute_extents();
        write_classes();
        write_tables(layer_table());
        write_blocks();
        write_entities();
        write_objects();
        Out o;
        header(o);  // last: $HANDSEED needs the final allocator state
        o.s.reserve(o.s.size() + classes.s.size() + tables.s.size() + blocks.s.size() +
                    ents.s.size() + objs.s.size() + 16);
        o.s += classes.s;
        o.s += tables.s;
        o.s += blocks.s;
        o.s += ents.s;
        o.s += objs.s;
        o.tag(0, "EOF");
        if (entity_count) *entity_count = count;
        return std::move(o.s);
    }
};

}  // namespace r2000

}  // namespace

std::string dxf_text_string(const std::string& utf8) {
    const std::string in = ink_expand_callouts(utf8);
    std::string out;
    out.reserve(in.size() + 4);
    for (std::size_t i = 0; i < in.size();) {
        const char32_t cp = ink_utf8_next(in, i);
        switch (cp) {
            case '%': out += "%%%"; continue;
            case 0x00B0: out += "%%d"; continue;
            case 0x00B1: out += "%%p"; continue;
            case 0x00D8: case 0x00F8: case 0x2300: out += "%%c"; continue;
            case 0x00D7: out += "X"; continue;
            case 0x2212: case 0x2013: case 0x2014: out += "-"; continue;
            case '\t': out += " "; continue;
            default: break;
        }
        if (cp >= 0x20 && cp <= 0x7E) out.push_back(static_cast<char>(cp));
        else if (cp >= 0xA0 && cp <= 0xFF) out.push_back(static_cast<char>(cp));  // cp1252
        else if (cp >= 0x20) out.push_back('?');
    }
    return out;
}

std::string build_dxf(const DxfDocument& doc, int* entity_count) {
    if (!doc.acis.empty()) return r2000::Writer(doc).build(entity_count);
    Out o;
    o.s.reserve(1 << 16);
    header(o, doc);
    tables(o, doc);
    const int n = entities(o, doc);
    o.tag(0, "EOF");
    if (entity_count) *entity_count = n;
    return std::move(o.s);
}

std::string write_dxf(const std::string& path, const DxfDocument& doc) {
    int n = 0;
    const std::string text = build_dxf(doc, &n);
    if (n == 0)
        throw std::runtime_error("write_dxf: no geometry to export (all layers empty)");
    std::filesystem::path p(path);
    if (p.has_parent_path()) std::filesystem::create_directories(p.parent_path());
    std::ofstream f(p, std::ios::binary | std::ios::trunc);
    if (!f) throw std::runtime_error("write_dxf: cannot open output: " + path);
    f.write(text.data(), static_cast<std::streamsize>(text.size()));
    f.close();
    if (!f) throw std::runtime_error("write_dxf: failed writing output: " + path);
    return std::filesystem::absolute(p).string();
}

}  // namespace chudware::doc
