// ACIS SAT writer. Record syntax mirrors what real producers write:
//   - 400: ODA's ACIS 4.00 output embedded in R2000 DWG/DXF (scratch/dwg_acis/sat_ref):
//     `type $attr <data> #`, edges without parameters, spline data without approximation level.
//   - 700: ACIS 7.0 / ASM output (Spatial "SAT Format 7.0", Inventor and SolidWorks .sat):
//     `type $attr -1 $pattern <data> #` for every entity, `edge ... $v0 t0 $v1 t1 ... @7 unknown`,
//     `{ exactsur full ...`, header strings length-prefixed without `@`.
// Both: `End-of-ACIS-data` terminator, null pointers `$-1`, body records first.
//
// Topology: body -> lump (one per solid; one per free shell / edge-connected group of free
// faces) -> shell -> face -> loop -> coedge -> edge -> vertex -> point.
//   - Faces of solids are `single`; faces of sheets `double out` (what AutoCAD writes for
//     SURFACE entities). Face sense is `reversed` exactly when the face's outward normal is
//     opposite to the written surface's normal, decided numerically per face.
//   - Loops: outer first, coedges in traversal order with metal on the left (the OCCT wire
//     order with orientations composed through face/shell/solid).
//   - Coedges of one edge form the partner ring; coedge sense is the edge's orientation in
//     the face. OCCT degenerate edges (sphere poles, cone apexes) are not written: ACIS needs
//     no edge there and the neighbouring coedges already meet at the singular vertex.
//   - Seam edges of cylinder/cone/sphere/torus faces are not written either (when no other
//     face uses them and the rest still forms closed loops): ACIS bounds a cylinder band by
//     its two circles and a whole sphere/torus by no loop at all, as AutoCAD stores them.
//   - Edges always run `forward` along their curve: start vertex at the OCCT first
//     parameter (or, for a recognised analytic curve, its projection), end vertex at the last.
// Geometry is written analytically whenever it exactly is analytic: B-spline/Bezier/other
// surfaces within 1e-7 of the body size of a plane, cylinder, cone, sphere or torus
// (GeomConvert_SurfToAnaSurf) and curves of a line, circle or ellipse
// (GeomConvert_CurveToAnaCurve, every sample of the span checked on the arc) take the
// analytic records below; only genuinely free-form geometry becomes NURBS.
// Records:
//   - plane -> plane-surface (normal = Xdir x Ydir, `forward_v`, so ACIS u/v = OCCT u/v);
//     cylinder/cone -> cone-surface (base ellipse, sin/cos of the half angle with cos > 0 so
//     the normal points away from the axis, u scale = base radius); sphere -> sphere-surface;
//     torus -> torus-surface (positive radii: outward normals). Any other surface ->
//     spline-surface `exactsur` holding the exact NURBS (B-spline/Bezier kept in their own
//     parameters, revolution/extrusion via GeomConvert over the face's UV box; offset
//     surfaces, which have no NURBS form, approximated within 1e-7 of the body size).
//     Periodic B-splines are written clamped (`closed` when the opposite boundaries
//     coincide); rational ones carry the `both` rational-direction word (as ODA and ACIS
//     write it at 4.00 and 7.00); collapsed boundaries are flagged as ACIS singularities
//     (`low`/`both`; a lone high-end singularity is moved to the low end by reversing that
//     parameter direction).
//   - line -> straight-curve (parameter = distance from the root along the unit direction),
//     circle/ellipse -> ellipse-curve (parameter = angle from the major axis), anything else
//     -> intcurve `exactcur` holding the exact NURBS of the edge's span.
//   - ACIS knot vectors store end multiplicity = degree (OCCT's degree + 1); poles are
//     written v-major (u varies fastest); discontinuity lists name the interior knots whose
//     multiplicity breaks C1/C2/C3.
//   - Pcurves: every coedge on a spline face gets an `exppc` pcurve (ACIS evaluates spline-face
//     boundaries in the surface's parameter space; every real 7.0 producer writes them).
//     Coedges on analytic faces get `$-1`, as ACIS/ODA write them. A pcurve is parameterised
//     by the coedge parameter (edge parameter, negated and swapped on a reversed coedge) and
//     references the face's spline surface by subtype index (`{ ref n }`, counted in file
//     order, outer subtype first). It is the OCCT pcurve itself when both parameterisations
//     are OCCT's, otherwise a C0-joined cubic Hermite fitted in the coedge parameter (its
//     measured deviation is the written fit tolerance).
// Self-checks before anything is returned (std::runtime_error names the entity on failure):
// every edge's curve evaluated at its written parameters lands on its vertices, every face
// point lies on the written surface, every pcurve maps onto its 3D curve within the
// written fit tolerance.
#include "doc/acis_write.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <cstring>
#include <functional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <variant>
#include <vector>

#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepTools.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <ElCLib.hxx>
#include <ElSLib.hxx>
#include <Extrema_GenLocateExtPS.hxx>
#include <Geom2dConvert.hxx>
#include <Geom2d_BSplineCurve.hxx>
#include <Geom2d_BezierCurve.hxx>
#include <Geom2d_Curve.hxx>
#include <Geom2d_Line.hxx>
#include <Geom2d_TrimmedCurve.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <GeomAdaptor_Surface.hxx>
#include <GeomConvert.hxx>
#include <GeomConvert_ApproxSurface.hxx>
#include <GeomConvert_CurveToAnaCurve.hxx>
#include <GeomConvert_SurfToAnaSurf.hxx>
#include <Geom_BSplineCurve.hxx>
#include <Geom_BSplineSurface.hxx>
#include <Geom_BezierCurve.hxx>
#include <Geom_BezierSurface.hxx>
#include <Geom_Circle.hxx>
#include <Geom_ConicalSurface.hxx>
#include <Geom_Curve.hxx>
#include <Geom_CylindricalSurface.hxx>
#include <Geom_Ellipse.hxx>
#include <Geom_Line.hxx>
#include <Geom_OffsetSurface.hxx>
#include <Geom_Plane.hxx>
#include <Geom_RectangularTrimmedSurface.hxx>
#include <Geom_SphericalSurface.hxx>
#include <Geom_Surface.hxx>
#include <Geom_ToroidalSurface.hxx>
#include <Geom_TrimmedCurve.hxx>
#include <Precision.hxx>
#include <Standard_Failure.hxx>
#include <TColStd_Array1OfInteger.hxx>
#include <TColStd_Array1OfReal.hxx>
#include <TColStd_Array2OfReal.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TColgp_Array1OfPnt2d.hxx>
#include <TColgp_Array2OfPnt.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_DataMapOfShapeInteger.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax3.hxx>
#include <gp_Circ.hxx>
#include <gp_Cone.hxx>
#include <gp_Elips.hxx>
#include <gp_Lin.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace chudware::doc {
namespace {

[[noreturn]] void fail(const std::string& what) {
    throw std::runtime_error("ACIS writer: " + what);
}

// Shortest text that reads back to the same double (lossless round trip).
std::string num(double v) {
    if (!std::isfinite(v)) fail("non-finite number in geometry");
    if (v == 0.0) return "0";
    char buf[40];
    const auto r = std::to_chars(buf, buf + sizeof(buf), v);
    return std::string(buf, r.ptr);
}

// ---------------------------------------------------------------------------------------
// NURBS data in ACIS conventions.

// Distinct knots with multiplicities; the end multiplicities are the degree (ACIS stores
// one copy fewer at each end than the clamped knot vector OCCT uses).
struct KnotVec {
    std::vector<double> k;
    std::vector<int> m;
};

struct BsCurve3 {
    int deg = 0;
    KnotVec kv;
    std::vector<gp_Pnt> p;
    std::vector<double> w;  // empty when polynomial
    bool closed = false;
};

struct BsCurve2 {
    int deg = 0;
    KnotVec kv;
    std::vector<gp_Pnt2d> p;
    std::vector<double> w;
    bool closed = false;
};

struct BsSurf {
    int udeg = 0, vdeg = 0;
    KnotVec uk, vk;
    int nu = 0, nv = 0;
    std::vector<gp_Pnt> p;  // v-major: index = j * nu + i (u varies fastest)
    std::vector<double> w;
    bool uclosed = false, vclosed = false;
    const char* using_ = "none";
    const char* vsing = "none";
};

template<class Knot, class Multiplicity>
KnotVec acis_knots(int nk, int deg, Knot&& knot, Multiplicity&& mult, const char* what) {
    KnotVec kv;
    for (int i = 1; i <= nk; ++i) {
        kv.k.push_back(knot(i));
        kv.m.push_back(mult(i));
    }
    if (kv.m.size() < 2 || kv.m.front() != deg + 1 || kv.m.back() != deg + 1)
        fail(std::string(what) + ": B-spline is not clamped at its ends");
    kv.m.front() = deg;
    kv.m.back() = deg;
    return kv;
}

bool all_equal(const std::vector<double>& w) {
    for (double x : w)
        if (std::fabs(x - w.front()) > 1e-14 * std::fabs(w.front())) return false;
    return true;
}

// OCCT clamped knots/mults rebuilt from ACIS conventions.
void occt_knots(const KnotVec& kv, TColStd_Array1OfReal& K, TColStd_Array1OfInteger& M) {
    const int n = static_cast<int>(kv.k.size());
    for (int i = 0; i < n; ++i) {
        K(i + 1) = kv.k[i];
        M(i + 1) = kv.m[i] + ((i == 0 || i == n - 1) ? 1 : 0);
    }
}

// Rebuilt from the written arrays, so every self-check exercises the ACIS conversion itself.
Handle(Geom_BSplineCurve) rebuild(const BsCurve3& b) {
    const int nk = static_cast<int>(b.kv.k.size());
    const int np = static_cast<int>(b.p.size());
    TColStd_Array1OfReal K(1, nk);
    TColStd_Array1OfInteger M(1, nk);
    occt_knots(b.kv, K, M);
    TColgp_Array1OfPnt P(1, np);
    for (int i = 0; i < np; ++i) P(i + 1) = b.p[i];
    if (b.w.empty()) return new Geom_BSplineCurve(P, K, M, b.deg);
    TColStd_Array1OfReal W(1, np);
    for (int i = 0; i < np; ++i) W(i + 1) = b.w[i];
    return new Geom_BSplineCurve(P, W, K, M, b.deg);
}

Handle(Geom2d_BSplineCurve) rebuild(const BsCurve2& b) {
    const int nk = static_cast<int>(b.kv.k.size());
    const int np = static_cast<int>(b.p.size());
    TColStd_Array1OfReal K(1, nk);
    TColStd_Array1OfInteger M(1, nk);
    occt_knots(b.kv, K, M);
    TColgp_Array1OfPnt2d P(1, np);
    for (int i = 0; i < np; ++i) P(i + 1) = b.p[i];
    if (b.w.empty()) return new Geom2d_BSplineCurve(P, K, M, b.deg);
    TColStd_Array1OfReal W(1, np);
    for (int i = 0; i < np; ++i) W(i + 1) = b.w[i];
    return new Geom2d_BSplineCurve(P, W, K, M, b.deg);
}

Handle(Geom_BSplineSurface) rebuild(const BsSurf& b) {
    const int nuk = static_cast<int>(b.uk.k.size()), nvk = static_cast<int>(b.vk.k.size());
    TColStd_Array1OfReal UK(1, nuk), VK(1, nvk);
    TColStd_Array1OfInteger UM(1, nuk), VM(1, nvk);
    occt_knots(b.uk, UK, UM);
    occt_knots(b.vk, VK, VM);
    TColgp_Array2OfPnt P(1, b.nu, 1, b.nv);
    TColStd_Array2OfReal W(1, b.nu, 1, b.nv);
    for (int j = 0; j < b.nv; ++j)
        for (int i = 0; i < b.nu; ++i) {
            P(i + 1, j + 1) = b.p[j * b.nu + i];
            W(i + 1, j + 1) = b.w.empty() ? 1.0 : b.w[j * b.nu + i];
        }
    if (b.w.empty()) return new Geom_BSplineSurface(P, UK, VK, UM, VM, b.udeg, b.vdeg);
    return new Geom_BSplineSurface(P, W, UK, VK, UM, VM, b.udeg, b.vdeg);
}

// Makes `c` non-periodic and clamped on exactly [f, l] (parameters unchanged).
template <class H>
void clamp_span(H& c, double f, double l, const char* what) {
    try {
        const double first = c->FirstParameter(), last = c->LastParameter();
        if (c->IsPeriodic()) {
            if (f >= first - Precision::PConfusion() && l <= last + Precision::PConfusion()) {
                c->SetNotPeriodic();
            } else {
                c->Segment(f, l);
                if (c->IsPeriodic()) c->SetNotPeriodic();
            }
        }
        f = std::max(f, c->FirstParameter());
        l = std::min(l, c->LastParameter());
        if (l - f <= Precision::PConfusion()) fail(std::string(what) + ": empty parameter span");
        const int deg = c->Degree();
        const bool clamped = c->Multiplicity(1) == deg + 1 &&
                             c->Multiplicity(c->NbKnots()) == deg + 1;
        if (!clamped || f > c->FirstParameter() + Precision::PConfusion() ||
            l < c->LastParameter() - Precision::PConfusion())
            c->Segment(f, l);
    } catch (const Standard_Failure& e) {
        fail(std::string(what) + ": B-spline segmentation failed: " + e.GetMessageString());
    }
}

BsCurve3 to_bs3(const Handle(Geom_BSplineCurve)& c, double tol, const char* what) {
    BsCurve3 b;
    b.deg = c->Degree();
    b.kv = acis_knots(c->NbKnots(), b.deg, [&](int i) { return c->Knot(i); },
                      [&](int i) { return c->Multiplicity(i); }, what);
    for (int i = 1; i <= c->NbPoles(); ++i) {
        b.p.push_back(c->Pole(i));
        b.w.push_back(c->Weight(i));
    }
    if (all_equal(b.w)) b.w.clear();
    b.closed = c->StartPoint().Distance(c->EndPoint()) <= tol;
    return b;
}

BsCurve2 to_bs2(const Handle(Geom2d_BSplineCurve)& c, double tol, const char* what) {
    BsCurve2 b;
    b.deg = c->Degree();
    b.kv = acis_knots(c->NbKnots(), b.deg, [&](int i) { return c->Knot(i); },
                      [&](int i) { return c->Multiplicity(i); }, what);
    for (int i = 1; i <= c->NbPoles(); ++i) {
        b.p.push_back(c->Pole(i));
        b.w.push_back(c->Weight(i));
    }
    if (all_equal(b.w)) b.w.clear();
    b.closed = c->StartPoint().Distance(c->EndPoint()) <= tol;
    return b;
}

// Parameter-space pcurve of a reversed coedge: bs2(s) for s in [-t1, -t0] traces the
// original backwards (ACIS presents coedge parameters (-b, -a) to the pcurve).
BsCurve2 negated(const BsCurve2& b) {
    BsCurve2 r = b;
    const size_t nk = b.kv.k.size(), np = b.p.size();
    for (size_t i = 0; i < nk; ++i) {
        r.kv.k[i] = -b.kv.k[nk - 1 - i];
        r.kv.m[i] = b.kv.m[nk - 1 - i];
    }
    for (size_t i = 0; i < np; ++i) r.p[i] = b.p[np - 1 - i];
    if (!b.w.empty())
        for (size_t i = 0; i < np; ++i) r.w[i] = b.w[np - 1 - i];
    return r;
}

// Interior knots whose multiplicity m leaves the spline C^(deg-m): listed under the first
// discontinuous derivative (1, 2 or 3), as ACIS discontinuity_info stores them.
std::array<std::vector<double>, 3> discontinuities(const KnotVec& kv, int deg) {
    std::array<std::vector<double>, 3> d;
    for (size_t i = 1; i + 1 < kv.k.size(); ++i) {
        const int c = deg - kv.m[i];
        if (c >= 0 && c <= 2) d[c].push_back(kv.k[i]);
    }
    return d;
}

// ---------------------------------------------------------------------------------------
// Records.

struct BodyR { int lump = -1; };
struct LumpR { int next = -1, shell = -1, body = -1; };
struct ShellR { int next = -1, face = -1, lump = -1; };
struct FaceR {
    int next = -1, loop = -1, shell = -1, surface = -1;
    bool reversed = false, dbl = false;
};
struct LoopR { int next = -1, coedge = -1, face = -1; };
struct CoedgeR {
    int next = -1, prev = -1, partner = -1, edge = -1, loop = -1, pcurve = -1;
    bool reversed = false;
};
struct EdgeR {
    int v0 = -1, v1 = -1, coedge = -1, curve = -1;
    double t0 = 0, t1 = 0;
};
struct VertexR { int edge = -1, point = -1; };
struct PointR { gp_Pnt p; };
struct StraightR { gp_Pnt root; gp_Dir dir; };
struct EllipseR { gp_Pnt center; gp_Dir normal; gp_Vec major; double ratio = 1; };
struct IntcurveR { BsCurve3 bs; };
struct PlaneR { gp_Pnt root; gp_Dir normal, udir; };
struct ConeR {
    gp_Pnt center; gp_Dir axis; gp_Vec major;
    double sine = 0, cosine = 1, scale = 1;
};
struct SphereR { gp_Pnt center; double radius = 1; gp_Dir udir, pole; };
struct TorusR { gp_Pnt center; gp_Dir axis; double major = 1, minor = 1; gp_Dir udir; };
struct SplineR { int surf = -1; };  // index into Writer::splines_
struct PcurveR { BsCurve2 bs; double fitol = 0; int surf = -1; };

using Rec = std::variant<BodyR, LumpR, ShellR, FaceR, LoopR, CoedgeR, EdgeR, VertexR, PointR,
                         StraightR, EllipseR, IntcurveR, PlaneR, ConeR, SphereR, TorusR,
                         SplineR, PcurveR>;

// Token stream with ACIS whitespace rules: records may span lines; lines stay well below the
// 255-character DXF group limit so the embedded form needs no continuation groups.
class Ser {
public:
    explicit Ser(int version) : version_(version) {}
    void tok(std::string_view t) {
        if (out_.size() > line_start_) {
            if (out_.size() - line_start_ + 1 + t.size() > kMaxLine) nl();
            else out_ += ' ';
        }
        out_ += t;
    }
    void real(double v) { tok(num(v)); }
    void integer(long long v) { tok(std::to_string(v)); }
    void ptr(int idx) { tok(idx < 0 ? std::string("$-1") : "$" + std::to_string(idx)); }
    void xyz(const gp_XYZ& p) { real(p.X()); real(p.Y()); real(p.Z()); }
    void nl() {
        if (out_.size() > line_start_) {
            out_ += '\n';
            line_start_ = out_.size();
        }
    }
    // Entity prefix: type, attribute pointer, and from 7.0 the entity id and pattern pointer.
    void begin(const char* type) {
        tok(type);
        tok("$-1");
        if (version_ >= 700) {
            tok("-1");
            tok("$-1");
        }
    }
    void end() { tok("#"); nl(); }
    int version() const { return version_; }
    int next_subtype() { return subtype_++; }
    std::unordered_map<int, int>& surf_subtype() { return surf_subtype_; }
    std::string take() { nl(); return std::move(out_); }

private:
    static constexpr size_t kMaxLine = 200;
    int version_;
    std::string out_;
    size_t line_start_ = 0;
    int subtype_ = 0;
    std::unordered_map<int, int> surf_subtype_;  // spline id -> subtype index of its definition
};

void put_disc(Ser& s, const KnotVec& kv, int deg) {
    for (const auto& list : discontinuities(kv, deg)) {
        s.integer(static_cast<long long>(list.size()));
        for (double v : list) s.real(v);
    }
}

void put_knots(Ser& s, const KnotVec& kv) {
    for (size_t i = 0; i < kv.k.size(); ++i) {
        s.real(kv.k[i]);
        s.integer(kv.m[i]);
    }
    s.nl();
}

void put_bs3(Ser& s, const BsCurve3& b) {
    s.tok(b.w.empty() ? "nubs" : "nurbs");
    s.integer(b.deg);
    s.tok(b.closed ? "closed" : "open");
    s.integer(static_cast<long long>(b.kv.k.size()));
    s.nl();
    put_knots(s, b.kv);
    for (size_t i = 0; i < b.p.size(); ++i) {
        s.xyz(b.p[i].XYZ());
        if (!b.w.empty()) s.real(b.w[i]);
        s.nl();
    }
}

void put_bs2(Ser& s, const BsCurve2& b) {
    s.tok(b.w.empty() ? "nubs" : "nurbs");
    s.integer(b.deg);
    s.tok(b.closed ? "closed" : "open");
    s.integer(static_cast<long long>(b.kv.k.size()));
    s.nl();
    put_knots(s, b.kv);
    for (size_t i = 0; i < b.p.size(); ++i) {
        s.real(b.p[i].X());
        s.real(b.p[i].Y());
        if (!b.w.empty()) s.real(b.w[i]);
        s.nl();
    }
}

void put_bs3_surface(Ser& s, const BsSurf& b) {
    if (b.w.empty()) {
        s.tok("nubs");
        s.integer(b.udeg);
        s.integer(b.vdeg);
    } else {
        s.tok("nurbs");
        s.integer(b.udeg);
        s.integer(b.vdeg);
        // Rational directions: real 7.0 files (ACIS 27 saving at 700) write `both` here; a
        // surface rational in both directions is exact for any weight net.
        s.tok("both");
    }
    s.tok(b.uclosed ? "closed" : "open");
    s.tok(b.vclosed ? "closed" : "open");
    s.tok(b.using_);
    s.tok(b.vsing);
    s.integer(static_cast<long long>(b.uk.k.size()));
    s.integer(static_cast<long long>(b.vk.k.size()));
    s.nl();
    put_knots(s, b.uk);
    put_knots(s, b.vk);
    for (size_t i = 0; i < b.p.size(); ++i) {
        s.xyz(b.p[i].XYZ());
        if (!b.w.empty()) s.real(b.w[i]);
        s.nl();
    }
}

// `{ exactsur ... }` on first use of spline `id`, `{ ref n }` afterwards.
void put_spline_subtype(Ser& s, int id, const BsSurf& b) {
    s.tok("{");
    auto& seen = s.surf_subtype();
    if (const auto it = seen.find(id); it != seen.end()) {
        s.tok("ref");
        s.integer(it->second);
        s.tok("}");
        return;
    }
    seen[id] = s.next_subtype();
    s.tok("exactsur");
    if (s.version() > 400) s.tok("full");  // approximation level, saved from ACIS 5.0
    put_bs3_surface(s, b);
    s.real(0.0);  // fit tolerance: the B-spline is the surface
    s.nl();
    put_disc(s, b.uk, b.udeg);
    put_disc(s, b.vk, b.vdeg);
    s.nl();
    for (const char* t : {"F", "1", "F", "0", "F", "1", "F", "0"}) s.tok(t);  // empty subsets
    s.tok("}");
}

// ---------------------------------------------------------------------------------------
// Geometry analysis.

struct EdgeGeom {
    enum class K { Line, Ellipse, Spline } k = K::Line;
    StraightR line;
    EllipseR ell;
    BsCurve3 bs;
    Handle(Geom_Curve) eval;  // the written curve, in the written (ACIS) parameterisation
    double t0 = 0, t1 = 0;    // written edge parameters
    bool occt_param = false;  // written parameter == OCCT edge parameter
    Handle(Geom_Curve) occt;  // located OCCT curve
    double f = 0, l = 0;      // OCCT edge range
};

struct SurfGeom {
    enum class K { Plane, Cone, Sphere, Torus, Spline } k = K::Plane;
    PlaneR plane;
    ConeR cone;
    SphereR sphere;
    TorusR torus;
    BsSurf bs;
    Handle(Geom_BSplineSurface) eval;  // the written spline
    bool occt_param = false;           // written (u,v) = flip(OCCT (u,v))
    bool flip_u = false, flip_v = false;
    double u1 = 0, u2 = 0, v1 = 0, v2 = 0;     // written spline domain
    double fu0 = 0, fu1 = 0, fv0 = 0, fv1 = 0;  // OCCT face UV box
};

gp_Pnt2d flip(const SurfGeom& g, gp_Pnt2d uv) {
    if (g.flip_u) uv.SetX(g.u1 + g.u2 - uv.X());
    if (g.flip_v) uv.SetY(g.v1 + g.v2 - uv.Y());
    return uv;
}

// Normal of the written ACIS surface at a point on it (unnormalised; zero when singular).
gp_Vec acis_normal(const SurfGeom& g, const gp_Pnt& P, const gp_Pnt2d* uv_written) {
    switch (g.k) {
    case SurfGeom::K::Plane: return gp_Vec(g.plane.normal);
    case SurfGeom::K::Cone: {
        const gp_Vec a(g.cone.axis);
        const gp_Vec w(g.cone.center, P);
        gp_Vec r = w - a * w.Dot(a);
        if (r.Magnitude() < Precision::Confusion()) return gp_Vec(0, 0, 0);
        r.Normalize();
        return r * g.cone.cosine - a * g.cone.sine;
    }
    case SurfGeom::K::Sphere: return gp_Vec(g.sphere.center, P);
    case SurfGeom::K::Torus: {
        const gp_Vec a(g.torus.axis);
        const gp_Vec w(g.torus.center, P);
        gp_Vec r = w - a * w.Dot(a);
        if (r.Magnitude() < Precision::Confusion()) return gp_Vec(0, 0, 0);
        r.Normalize();
        const gp_Pnt ring = g.torus.center.Translated(r * g.torus.major);
        return gp_Vec(ring, P);
    }
    case SurfGeom::K::Spline: {
        gp_Pnt Q;
        gp_Vec du, dv;
        g.eval->D1(uv_written->X(), uv_written->Y(), Q, du, dv);
        return du.Crossed(dv);
    }
    }
    return gp_Vec(0, 0, 0);
}

// Distance from P to the written analytic surface.
double analytic_distance(const SurfGeom& g, const gp_Pnt& P) {
    switch (g.k) {
    case SurfGeom::K::Plane:
        return std::fabs(gp_Vec(g.plane.root, P).Dot(gp_Vec(g.plane.normal)));
    case SurfGeom::K::Cone: {
        const gp_Vec a(g.cone.axis);
        const gp_Vec w(g.cone.center, P);
        const double h = w.Dot(a);
        const double rho = (w - a * h).Magnitude();
        // Generator through the base circle: radius grows by tan(angle) per unit of height.
        const double rad = g.cone.major.Magnitude() + h * g.cone.sine / g.cone.cosine;
        return std::fabs(rho - rad) * g.cone.cosine;
    }
    case SurfGeom::K::Sphere:
        return std::fabs(P.Distance(g.sphere.center) - g.sphere.radius);
    case SurfGeom::K::Torus: {
        const gp_Vec a(g.torus.axis);
        const gp_Vec w(g.torus.center, P);
        const double h = w.Dot(a);
        const double rho = (w - a * h).Magnitude();
        return std::fabs(std::hypot(rho - g.torus.major, h) - g.torus.minor);
    }
    case SurfGeom::K::Spline: break;
    }
    return 0.0;
}

// Written (u,v) of a point P on the written spline, starting from a guess. Gauss-Newton on
// |S(u,v) - P| converges to machine precision for points on the surface and, being local,
// keeps a guess on the correct side of a closed surface's boundary; singular points (a
// collapsed boundary) fall back to OCCT's extremum search.
gp_Pnt2d locate_on_spline(const SurfGeom& g, const gp_Pnt& P, gp_Pnt2d guess) {
    double u = std::clamp(guess.X(), g.u1, g.u2), v = std::clamp(guess.Y(), g.v1, g.v2);
    const double span = std::max(g.u2 - g.u1, g.v2 - g.v1);
    for (int it = 0; it < 60; ++it) {
        gp_Pnt S;
        gp_Vec su, sv;
        g.eval->D1(u, v, S, su, sv);
        const gp_Vec r(S, P);
        const double a = su.Dot(su), b = su.Dot(sv), c = sv.Dot(sv);
        const double det = a * c - b * b;
        if (!(det > 1e-20 * a * c)) break;
        const double ru = su.Dot(r), rv = sv.Dot(r);
        const double nu = std::clamp(u + (c * ru - b * rv) / det, g.u1, g.u2);
        const double nv = std::clamp(v + (a * rv - b * ru) / det, g.v1, g.v2);
        const bool still = std::fabs(nu - u) + std::fabs(nv - v) <= 1e-15 * span;
        u = nu;
        v = nv;
        if (still) return gp_Pnt2d(u, v);
    }
    const double d_gn = g.eval->Value(u, v).Distance(P);
    GeomAdaptor_Surface ad(g.eval);
    Extrema_GenLocateExtPS loc(ad);
    loc.Perform(P, u, v);
    if (loc.IsDone()) {
        double eu, ev;
        loc.Point().Parameter(eu, ev);
        if (g.eval->Value(eu, ev).Distance(P) < d_gn) return gp_Pnt2d(eu, ev);
    }
    return gp_Pnt2d(u, v);
}

// Written (u,v) of the OCCT face parameters (u,v) and its 3D point P.
gp_Pnt2d written_uv(const SurfGeom& g, const gp_Pnt2d& occt_uv, const gp_Pnt& P) {
    if (g.occt_param) return flip(g, occt_uv);
    // GeomConvert maps the face's UV box monotonically onto the spline domain: the linear map
    // lands on the right side of any closed boundary and close to the answer.
    auto lin = [](double x, double a0, double a1, double b0, double b1) {
        return a1 - a0 > 0 ? b0 + (x - a0) / (a1 - a0) * (b1 - b0) : b0;
    };
    gp_Pnt2d guess(lin(occt_uv.X(), g.fu0, g.fu1, g.u1, g.u2),
                   lin(occt_uv.Y(), g.fv0, g.fv1, g.v1, g.v2));
    if (g.flip_u) guess.SetX(g.u1 + g.u2 - guess.X());
    if (g.flip_v) guess.SetY(g.v1 + g.v2 - guess.Y());
    return locate_on_spline(g, P, guess);
}

bool rows_collapse(const Handle(Geom_BSplineSurface)& b, bool u_side, bool high, double tol) {
    const int n = u_side ? b->NbVPoles() : b->NbUPoles();
    const int fixed = u_side ? (high ? b->NbUPoles() : 1) : (high ? b->NbVPoles() : 1);
    const gp_Pnt first = u_side ? b->Pole(fixed, 1) : b->Pole(1, fixed);
    for (int j = 2; j <= n; ++j) {
        const gp_Pnt q = u_side ? b->Pole(fixed, j) : b->Pole(j, fixed);
        if (q.Distance(first) > tol) return false;
    }
    return true;
}

bool sides_coincide(const Handle(Geom_BSplineSurface)& b, bool u_side, double tol) {
    const int n = u_side ? b->NbVPoles() : b->NbUPoles();
    for (int j = 1; j <= n; ++j) {
        const gp_Pnt a = u_side ? b->Pole(1, j) : b->Pole(j, 1);
        const gp_Pnt c = u_side ? b->Pole(b->NbUPoles(), j) : b->Pole(j, b->NbVPoles());
        if (a.Distance(c) > tol) return false;
    }
    return true;
}

std::string type_name(const Handle(Standard_Transient)& t) {
    return t.IsNull() ? std::string("null") : std::string(t->DynamicType()->Name());
}

bool is_analytic(const Handle(Geom_Surface)& s) {
    return s->IsKind(STANDARD_TYPE(Geom_Plane)) || s->IsKind(STANDARD_TYPE(Geom_CylindricalSurface)) ||
           s->IsKind(STANDARD_TYPE(Geom_ConicalSurface)) || s->IsKind(STANDARD_TYPE(Geom_SphericalSurface)) ||
           s->IsKind(STANDARD_TYPE(Geom_ToroidalSurface));
}

// The line, circle or ellipse that `c` is over [f, l] within `tol`, oriented so its parameter
// grows with c's, plus the parameters t0 < t1 of c(f) and c(l) on it; null when c is free-form
// there. Every sample of the span must lie on the result, in increasing order inside
// [t0, t1], so a conic is never written with the complementary arc.
Handle(Geom_Curve) recognize_curve(const Handle(Geom_Curve)& c, double f, double l, double tol,
                                   double& t0, double& t1) {
    Handle(Geom_Curve) ana;
    try {
        GeomConvert_CurveToAnaCurve conv(c);
        conv.SetConvType(GeomConvert_Simplest);
        double nf = 0, nl = 0;
        if (!conv.ConvertToAnalytical(tol, ana, f, l, nf, nl) || ana.IsNull()) return nullptr;
    } catch (const Standard_Failure&) {
        return nullptr;
    }
    while (auto t = Handle(Geom_TrimmedCurve)::DownCast(ana)) ana = t->BasisCurve();
    gp_Pnt Pm;
    gp_Vec Vm;
    c->D1(0.5 * (f + l), Pm, Vm);
    if (Vm.Magnitude() < Precision::Confusion()) return nullptr;
    constexpr double kTwoPi = 6.28318530717958647692;
    bool periodic = true;
    std::function<double(const gp_Pnt&)> param;
    if (auto ln = Handle(Geom_Line)::DownCast(ana)) {
        gp_Lin lin = ln->Lin();
        if (Vm.Dot(gp_Vec(lin.Direction())) < 0) lin.Reverse();
        ana = new Geom_Line(lin);
        param = [lin](const gp_Pnt& P) { return ElCLib::Parameter(lin, P); };
        periodic = false;
    } else if (auto ci = Handle(Geom_Circle)::DownCast(ana)) {
        gp_Circ circ = ci->Circ();
        const gp_Vec n(circ.Axis().Direction());
        if (Vm.Dot(n.Crossed(gp_Vec(circ.Location(), Pm))) < 0)
            circ.SetPosition(gp_Ax2(circ.Location(), circ.Axis().Direction().Reversed(), circ.XAxis().Direction()));
        ana = new Geom_Circle(circ);
        param = [circ](const gp_Pnt& P) { return ElCLib::Parameter(circ, P); };
    } else if (auto el = Handle(Geom_Ellipse)::DownCast(ana)) {
        gp_Elips elips = el->Elips();
        const gp_Vec n(elips.Axis().Direction());
        if (Vm.Dot(n.Crossed(gp_Vec(elips.Location(), Pm))) < 0)
            elips.SetPosition(gp_Ax2(elips.Location(), elips.Axis().Direction().Reversed(), elips.XAxis().Direction()));
        ana = new Geom_Ellipse(elips);
        param = [elips](const gp_Pnt& P) { return ElCLib::Parameter(elips, P); };
    } else {
        return nullptr;  // parabola/hyperbola: no ACIS record, stays a spline
    }
    const gp_Pnt P0 = c->Value(f), P1 = c->Value(l);
    t0 = param(P0);
    auto unwrap = [&](double p) {
        if (!periodic) return p;
        double d = std::fmod(p - t0, kTwoPi);
        if (d < 0) d += kTwoPi;
        return t0 + d;
    };
    t1 = unwrap(param(P1));
    if (periodic && (P0.Distance(P1) <= tol || t1 - t0 <= Precision::PConfusion())) t1 = t0 + kTwoPi;
    if (t1 - t0 <= Precision::PConfusion()) return nullptr;
    const double slack = 1e-9 * (t1 - t0);
    double prev = t0;
    constexpr int kSamples = 32;
    for (int i = 1; i < kSamples; ++i) {
        const gp_Pnt P = c->Value(f + (l - f) * i / kSamples);
        const double p = unwrap(param(P));
        if (p < prev - slack || p > t1 + slack || ana->Value(p).Distance(P) > tol) return nullptr;
        prev = p;
    }
    if (ana->Value(t0).Distance(P0) > tol || ana->Value(t1).Distance(P1) > tol) return nullptr;
    return ana;
}

// ---------------------------------------------------------------------------------------

struct ShellSpec {
    std::vector<TopoDS_Face> faces;  // orientation composed down from the body
    bool solid = false;
};
struct LumpSpec {
    std::vector<ShellSpec> shells;
};

std::vector<LumpSpec> collect_lumps(const TopoDS_Shape& shape) {
    std::vector<LumpSpec> lumps;
    for (TopExp_Explorer xs(shape, TopAbs_SOLID); xs.More(); xs.Next()) {
        LumpSpec lump;
        for (TopExp_Explorer xsh(xs.Current(), TopAbs_SHELL); xsh.More(); xsh.Next()) {
            ShellSpec sh;
            sh.solid = true;
            for (TopExp_Explorer xf(xsh.Current(), TopAbs_FACE); xf.More(); xf.Next())
                sh.faces.push_back(TopoDS::Face(xf.Current()));
            if (!sh.faces.empty()) lump.shells.push_back(std::move(sh));
        }
        if (TopExp_Explorer(xs.Current(), TopAbs_FACE, TopAbs_SHELL).More())
            fail("solid holds faces outside any shell");
        if (!lump.shells.empty()) lumps.push_back(std::move(lump));
    }
    for (TopExp_Explorer xsh(shape, TopAbs_SHELL, TopAbs_SOLID); xsh.More(); xsh.Next()) {
        ShellSpec sh;
        for (TopExp_Explorer xf(xsh.Current(), TopAbs_FACE); xf.More(); xf.Next())
            sh.faces.push_back(TopoDS::Face(xf.Current()));
        if (!sh.faces.empty()) lumps.push_back(LumpSpec{{std::move(sh)}});
    }
    // Free faces: one sheet lump per edge-connected group.
    std::vector<TopoDS_Face> free;
    for (TopExp_Explorer xf(shape, TopAbs_FACE, TopAbs_SHELL); xf.More(); xf.Next())
        free.push_back(TopoDS::Face(xf.Current()));
    if (!free.empty()) {
        std::vector<int> parent(free.size());
        for (size_t i = 0; i < free.size(); ++i) parent[i] = static_cast<int>(i);
        auto root = [&](int i) {
            while (parent[i] != i) i = parent[i] = parent[parent[i]];
            return i;
        };
        TopTools_DataMapOfShapeInteger owner;
        for (size_t i = 0; i < free.size(); ++i)
            for (TopExp_Explorer xe(free[i], TopAbs_EDGE); xe.More(); xe.Next()) {
                if (const int* o = owner.Seek(xe.Current())) parent[root(*o)] = root(static_cast<int>(i));
                else owner.Bind(xe.Current(), static_cast<int>(i));
            }
        std::vector<int> group_of(free.size(), -1);
        for (size_t i = 0; i < free.size(); ++i) {
            const int r = root(static_cast<int>(i));
            if (group_of[r] < 0) {
                group_of[r] = static_cast<int>(lumps.size());
                lumps.push_back(LumpSpec{{ShellSpec{}}});
            }
            lumps[group_of[r]].shells.front().faces.push_back(free[i]);
        }
    }
    if (TopExp_Explorer(shape, TopAbs_EDGE, TopAbs_FACE).More())
        fail("body contains free edges (wire geometry); only faces, shells and solids can be written");
    if (TopExp_Explorer(shape, TopAbs_VERTEX, TopAbs_EDGE).More())
        fail("body contains free vertices; only faces, shells and solids can be written");
    return lumps;
}

class Writer {
public:
    explicit Writer(const SatWriteOptions& opt) : opt_(opt) {}

    std::string run(const std::vector<TopoDS_Shape>& shapes) {
        std::vector<const TopoDS_Shape*> bodies;
        for (const auto& s : shapes)
            if (!s.IsNull()) bodies.push_back(&s);
        if (bodies.empty()) fail("no non-null shape to write");
        // Top-level entities come first in a save file.
        for (size_t i = 0; i < bodies.size(); ++i) push(BodyR{});
        for (size_t i = 0; i < bodies.size(); ++i) add_body(*bodies[i], static_cast<int>(i));
        return serialize(static_cast<int>(bodies.size()));
    }

private:
    struct EdgeInfo {
        int rec = -1;
        TopoDS_Edge edge;  // FORWARD
        EdgeGeom g;
        std::vector<int> coedges;
    };

    SatWriteOptions opt_;
    std::vector<Rec> recs_;
    std::vector<BsSurf> splines_;
    std::vector<EdgeInfo> edges_;
    TopTools_DataMapOfShapeInteger edge_ix_;    // edge -> index in edges_
    TopTools_DataMapOfShapeInteger vertex_rec_;  // vertex -> vertex record
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces_;  // edge -> faces using it (seams)
    double scale_ = 1.0;                        // body size: relative tolerances
    int face_no_ = 0;                           // running face number for messages

    int push(Rec r) {
        recs_.push_back(std::move(r));
        return static_cast<int>(recs_.size()) - 1;
    }
    template <class T> T& at(int i) { return std::get<T>(recs_[i]); }

    double tight() const { return 1e-7 * scale_; }

    void add_body(const TopoDS_Shape& shape, int body_rec) {
        Bnd_Box box;
        BRepBndLib::Add(shape, box, false);
        scale_ = box.IsVoid() ? 1.0 : std::max(std::sqrt(box.SquareExtent()), 1e-9);
        edges_.clear();
        edge_ix_.Clear();
        vertex_rec_.Clear();
        edge_faces_.Clear();
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces_);

        const std::vector<LumpSpec> lumps = collect_lumps(shape);
        if (lumps.empty()) fail("body has no faces");
        int prev_lump = -1;
        for (const LumpSpec& lump : lumps) {
            const int lrec = push(LumpR{});
            at<LumpR>(lrec).body = body_rec;
            if (prev_lump < 0) at<BodyR>(body_rec).lump = lrec;
            else at<LumpR>(prev_lump).next = lrec;
            prev_lump = lrec;
            int prev_shell = -1;
            for (const ShellSpec& sh : lump.shells) {
                const int srec = push(ShellR{});
                at<ShellR>(srec).lump = lrec;
                if (prev_shell < 0) at<LumpR>(lrec).shell = srec;
                else at<ShellR>(prev_shell).next = srec;
                prev_shell = srec;
                int prev_face = -1;
                for (const TopoDS_Face& f : sh.faces) {
                    const int frec = add_face(f, !sh.solid, srec);
                    if (prev_face < 0) at<ShellR>(srec).face = frec;
                    else at<FaceR>(prev_face).next = frec;
                    prev_face = frec;
                }
            }
        }
        // Partner rings: every coedge of an edge points at the next one.
        for (const EdgeInfo& e : edges_) {
            const size_t n = e.coedges.size();
            if (n < 2) continue;
            for (size_t i = 0; i < n; ++i) at<CoedgeR>(e.coedges[i]).partner = e.coedges[(i + 1) % n];
        }
    }

    // ---- surfaces --------------------------------------------------------------------

    SurfGeom build_surface(const TopoDS_Face& F) {
        const std::string where = "face " + std::to_string(face_no_);
        SurfGeom g;
        TopLoc_Location loc;
        Handle(Geom_Surface) s = BRep_Tool::Surface(F, loc);
        if (s.IsNull()) fail(where + " has no surface");
        BRepTools::UVBounds(F, g.fu0, g.fu1, g.fv0, g.fv1);
        bool rigid = true;
        if (!loc.IsIdentity()) {
            const gp_Trsf T = loc.Transformation();
            rigid = std::fabs(std::fabs(T.ScaleFactor()) - 1.0) < 1e-12;
            s->TransformParameters(g.fu0, g.fv0, T);
            s->TransformParameters(g.fu1, g.fv1, T);
            s = Handle(Geom_Surface)::DownCast(s->Transformed(T));
        }
        while (auto t = Handle(Geom_RectangularTrimmedSurface)::DownCast(s)) s = t->BasisSurface();
        if (auto off = Handle(Geom_OffsetSurface)::DownCast(s)) {
            // Offsets of canonical surfaces are canonical surfaces.
            if (Handle(Geom_Surface) eq = off->Surface(); !eq.IsNull()) {
                s = eq;
                rigid = false;  // parameterisation of the equivalent may differ
            }
        }
        // Free-form forms of analytic surfaces (NURBS boxes, imported B-spline cylinders...) are
        // written as the analytic record: ACIS/ASM treats those as exact and robust, and the
        // AutoCAD original stores them that way.
        if (!is_analytic(s)) {
            try {
                // The bounded form refuses some full-period faces (e.g. a whole NURBS cylinder)
                // that the unbounded form recognises; both report their maximal deviation.
                GeomConvert_SurfToAnaSurf conv(s);
                conv.SetConvType(GeomConvert_Simplest);
                Handle(Geom_Surface) ana = conv.ConvertToAnalytical(tight(), g.fu0, g.fu1, g.fv0, g.fv1);
                if (ana.IsNull() || !is_analytic(ana) || conv.Gap() > tight()) ana = conv.ConvertToAnalytical(tight());
                if (!ana.IsNull() && is_analytic(ana) && conv.Gap() <= tight()) s = ana;
            } catch (const Standard_Failure&) {
                // Not recognised: written as a spline below.
            }
        }

        if (auto pl = Handle(Geom_Plane)::DownCast(s)) {
            const gp_Ax3 ax = pl->Position();
            g.k = SurfGeom::K::Plane;
            g.plane.root = ax.Location();
            g.plane.udir = ax.XDirection();
            g.plane.normal = ax.XDirection().Crossed(ax.YDirection());
        } else if (auto cy = Handle(Geom_CylindricalSurface)::DownCast(s)) {
            const gp_Ax3 ax = cy->Position();
            g.k = SurfGeom::K::Cone;
            g.cone.center = ax.Location();
            g.cone.axis = ax.Direction();
            g.cone.major = gp_Vec(ax.XDirection()) * cy->Radius();
            g.cone.sine = 0.0;
            g.cone.cosine = 1.0;
            g.cone.scale = cy->Radius();
        } else if (auto co = Handle(Geom_ConicalSurface)::DownCast(s)) {
            const gp_Ax3 ax = co->Position();
            const double a = co->SemiAngle();
            // ACIS needs a base ellipse of positive radius: when OCCT's reference circle is
            // (near) the apex, move the base to the face's middle parameter line, measured on
            // this cone (the face's UV box may belong to a converted surface).
            double v = 0.0;
            if (co->RefRadius() <= tight()) {
                BRepAdaptor_Surface fa(F);
                double fu0, fu1, fv0, fv1;  // the face's own parameters (before any location)
                BRepTools::UVBounds(F, fu0, fu1, fv0, fv1);
                double vlo = 1e300, vhi = -1e300;
                for (int i = 0; i <= 4; ++i)
                    for (int j = 0; j <= 4; ++j) {
                        const gp_Pnt P = fa.Value(fu0 + (fu1 - fu0) * i / 4, fv0 + (fv1 - fv0) * j / 4);
                        double pu, pv;
                        ElSLib::Parameters(co->Cone(), P, pu, pv);
                        vlo = std::min(vlo, pv);
                        vhi = std::max(vhi, pv);
                    }
                const double vm = 0.5 * (vlo + vhi);
                v = co->RefRadius() + vm * std::sin(a) > tight() ? vm
                    : (co->RefRadius() + vhi * std::sin(a) > co->RefRadius() + vlo * std::sin(a) ? vhi : vlo);
            }
            const double r = co->RefRadius() + v * std::sin(a);
            if (r <= tight()) fail(where + ": cone face lies at its apex");
            g.k = SurfGeom::K::Cone;
            g.cone.center = ax.Location().Translated(gp_Vec(ax.Direction()) * (v * std::cos(a)));
            g.cone.axis = ax.Direction();
            g.cone.major = gp_Vec(ax.XDirection()) * r;
            g.cone.sine = std::sin(a);
            g.cone.cosine = std::cos(a);
            g.cone.scale = r;
        } else if (auto sp = Handle(Geom_SphericalSurface)::DownCast(s)) {
            const gp_Ax3 ax = sp->Position();
            g.k = SurfGeom::K::Sphere;
            g.sphere.center = ax.Location();
            g.sphere.radius = sp->Radius();
            g.sphere.udir = ax.XDirection();
            g.sphere.pole = ax.Direction();
        } else if (auto to = Handle(Geom_ToroidalSurface)::DownCast(s)) {
            const gp_Ax3 ax = to->Position();
            g.k = SurfGeom::K::Torus;
            g.torus.center = ax.Location();
            g.torus.axis = ax.Direction();
            g.torus.major = to->MajorRadius();
            g.torus.minor = to->MinorRadius();
            g.torus.udir = ax.XDirection();
        } else {
            build_spline(g, s, rigid, where);
        }
        return g;
    }

    void build_spline(SurfGeom& g, const Handle(Geom_Surface)& s, bool rigid, const std::string& where) {
        g.k = SurfGeom::K::Spline;
        Handle(Geom_BSplineSurface) b;
        try {
            if (auto bs = Handle(Geom_BSplineSurface)::DownCast(s)) {
                b = Handle(Geom_BSplineSurface)::DownCast(bs->Copy());
                g.occt_param = rigid;
            } else if (auto bz = Handle(Geom_BezierSurface)::DownCast(s)) {
                b = GeomConvert::SurfaceToBSplineSurface(bz);
                g.occt_param = rigid;
            } else if (Handle(Geom_OffsetSurface)::DownCast(s)) {
                // No exact NURBS form exists: approximate over the face's UV box (same
                // parameters) an order of magnitude inside the relative tolerance the edges
                // are checked against.
                Handle(Geom_RectangularTrimmedSurface) tr =
                    new Geom_RectangularTrimmedSurface(s, g.fu0, g.fu1, g.fv0, g.fv1);
                GeomConvert_ApproxSurface ap(tr, 0.1 * tight(), GeomAbs_C2, GeomAbs_C2, 11, 11, 400, 1);
                if (!ap.HasResult() || ap.MaxError() > tight())
                    fail(where + ": offset surface cannot be approximated within " + num(tight()) +
                         (ap.HasResult() ? " (reached " + num(ap.MaxError()) + ")" : std::string()));
                b = ap.Surface();
                g.occt_param = rigid;
            } else {
                // Revolution / extrusion: exact rational conversion over the face's UV box.
                Handle(Geom_RectangularTrimmedSurface) tr =
                    new Geom_RectangularTrimmedSurface(s, g.fu0, g.fu1, g.fv0, g.fv1);
                b = GeomConvert::SurfaceToBSplineSurface(tr);
                g.occt_param = false;
            }
            if (b.IsNull()) fail(where + ": no B-spline form for " + type_name(s));
            clamp_surface(b, g, where);
        } catch (const Standard_Failure& e) {
            fail(where + ": cannot convert " + type_name(s) + " to an exact B-spline surface: " +
                 e.GetMessageString());
        }
        const double tol = tight();
        // ACIS singularity flags; a lone high-end collapse is moved to the low end.
        bool ulo = rows_collapse(b, true, false, tol), uhi = rows_collapse(b, true, true, tol);
        bool vlo = rows_collapse(b, false, false, tol), vhi = rows_collapse(b, false, true, tol);
        b->Bounds(g.u1, g.u2, g.v1, g.v2);
        if (uhi && !ulo) {
            b->UReverse();
            g.flip_u = true;
            std::swap(ulo, uhi);
        }
        if (vhi && !vlo) {
            b->VReverse();
            g.flip_v = true;
            std::swap(vlo, vhi);
        }
        BsSurf& o = g.bs;
        o.udeg = b->UDegree();
        o.vdeg = b->VDegree();
        o.uk = acis_knots(b->NbUKnots(), o.udeg, [&](int i) { return b->UKnot(i); },
                          [&](int i) { return b->UMultiplicity(i); }, where.c_str());
        o.vk = acis_knots(b->NbVKnots(), o.vdeg, [&](int i) { return b->VKnot(i); },
                          [&](int i) { return b->VMultiplicity(i); }, where.c_str());
        o.nu = b->NbUPoles();
        o.nv = b->NbVPoles();
        for (int j = 1; j <= o.nv; ++j)
            for (int i = 1; i <= o.nu; ++i) {
                o.p.push_back(b->Pole(i, j));
                o.w.push_back(b->Weight(i, j));
            }
        if (all_equal(o.w)) o.w.clear();
        o.uclosed = sides_coincide(b, true, tol);
        o.vclosed = sides_coincide(b, false, tol);
        o.using_ = ulo && uhi ? "both" : (ulo ? "low" : "none");
        o.vsing = vlo && vhi ? "both" : (vlo ? "low" : "none");
        g.eval = rebuild(o);
        g.eval->Bounds(g.u1, g.u2, g.v1, g.v2);
    }

    // Non-periodic, clamped B-spline covering the face (parameters unchanged).
    void clamp_surface(Handle(Geom_BSplineSurface)& b, const SurfGeom& g, const std::string& where) {
        double U1, U2, V1, V2;
        b->Bounds(U1, U2, V1, V2);
        auto span = [](bool periodic, double period, double lo, double hi, double f0, double f1,
                       double& a, double& c) {
            if (periodic) {
                a = f0;
                c = std::min(f1, f0 + period);
                if (f0 >= lo - Precision::PConfusion() && f1 <= hi + Precision::PConfusion()) {
                    a = lo;
                    c = hi;
                }
            } else {
                a = lo;
                c = hi;
            }
        };
        double a0, a1, b0, b1;
        span(b->IsUPeriodic(), b->IsUPeriodic() ? b->UPeriod() : 0.0, U1, U2, g.fu0, g.fu1, a0, a1);
        span(b->IsVPeriodic(), b->IsVPeriodic() ? b->VPeriod() : 0.0, V1, V2, g.fv0, g.fv1, b0, b1);
        if (b->IsUPeriodic() && a0 == U1 && a1 == U2) b->SetUNotPeriodic();
        if (b->IsVPeriodic() && b0 == V1 && b1 == V2) b->SetVNotPeriodic();
        auto clamped = [&] {
            return b->UMultiplicity(1) == b->UDegree() + 1 &&
                   b->UMultiplicity(b->NbUKnots()) == b->UDegree() + 1 &&
                   b->VMultiplicity(1) == b->VDegree() + 1 &&
                   b->VMultiplicity(b->NbVKnots()) == b->VDegree() + 1;
        };
        if (b->IsUPeriodic() || b->IsVPeriodic() || !clamped()) {
            b->Segment(a0, a1, b0, b1);
            if (b->IsUPeriodic()) b->SetUNotPeriodic();
            if (b->IsVPeriodic()) b->SetVNotPeriodic();
        }
        if (!clamped()) fail(where + ": B-spline surface cannot be clamped");
    }

    // Decides the ACIS face sense and verifies the written surface passes through the face.
    bool face_reversed(const TopoDS_Face& F, const SurfGeom& g, const std::string& where) {
        BRepAdaptor_Surface ad(F, false);
        const double tol = std::max(tight(), BRep_Tool::Tolerance(F));
        for (int iu = 1; iu <= 5; ++iu)
            for (int iv = 1; iv <= 5; ++iv) {
                const double u = g.fu0 + (g.fu1 - g.fu0) * (iu * 2 - 1) / 10.0;
                const double v = g.fv0 + (g.fv1 - g.fv0) * ((iv * 3) % 5 + 0.5) / 5.0;
                gp_Pnt P;
                gp_Vec du, dv;
                ad.D1(u, v, P, du, dv);
                const gp_Vec n_occt = du.Crossed(dv);
                gp_Pnt2d uvw;
                double dist = 0.0;
                if (g.k == SurfGeom::K::Spline) {
                    uvw = written_uv(g, gp_Pnt2d(u, v), P);
                    dist = g.eval->Value(uvw.X(), uvw.Y()).Distance(P);
                } else {
                    dist = analytic_distance(g, P);
                }
                if (dist > tol)
                    fail(where + ": written surface misses the face by " + num(dist) + " (" +
                         type_name(ad.Surface().Surface()) + ")");
                const gp_Vec n_acis = acis_normal(g, P, &uvw);
                const double m = n_occt.Magnitude() * n_acis.Magnitude();
                if (m <= 0.0) continue;
                const double c = n_occt.Dot(n_acis) / m;
                if (std::fabs(c) < 0.9) continue;  // near a singular point: try another sample
                const bool same = c > 0;
                return (F.Orientation() == TopAbs_REVERSED) == same;
            }
        fail(where + ": no regular point to orient the face against its surface");
    }

    // ---- edges -----------------------------------------------------------------------

    EdgeGeom build_edge(const TopoDS_Edge& E, const std::string& where) {
        EdgeGeom g;
        TopLoc_Location loc;
        double f, l;
        Handle(Geom_Curve) c = BRep_Tool::Curve(E, loc, f, l);
        if (c.IsNull()) fail(where + " has no 3D curve");
        if (!loc.IsIdentity()) {
            const gp_Trsf T = loc.Transformation();
            f = c->TransformedParameter(f, T);
            l = c->TransformedParameter(l, T);
            c = Handle(Geom_Curve)::DownCast(c->Transformed(T));
        }
        g.occt = c;
        g.f = f;
        g.l = l;
        Handle(Geom_Curve) base = c;
        while (auto t = Handle(Geom_TrimmedCurve)::DownCast(base)) base = t->BasisCurve();
        // Free-form curves that are exactly lines, circles or ellipses are written as those
        // records, with the edge parameters taken on the analytic curve.
        bool recognized = false;
        if (Handle(Geom_Line)::DownCast(base).IsNull() && Handle(Geom_Circle)::DownCast(base).IsNull() &&
            Handle(Geom_Ellipse)::DownCast(base).IsNull()) {
            double t0 = 0, t1 = 0;
            if (Handle(Geom_Curve) ana = recognize_curve(c, f, l, tight(), t0, t1); !ana.IsNull()) {
                base = ana;
                f = t0;
                l = t1;
                recognized = true;
            }
        }

        if (auto ln = Handle(Geom_Line)::DownCast(base)) {
            g.k = EdgeGeom::K::Line;
            g.line.root = ln->Position().Location();
            g.line.dir = ln->Position().Direction();
            g.t0 = f;
            g.t1 = l;
            g.occt_param = true;
            g.eval = new Geom_Line(g.line.root, g.line.dir);
        } else if (auto ci = Handle(Geom_Circle)::DownCast(base)) {
            const gp_Ax2 ax = ci->Position();
            g.k = EdgeGeom::K::Ellipse;
            g.ell.center = ax.Location();
            g.ell.normal = ax.Direction();
            g.ell.major = gp_Vec(ax.XDirection()) * ci->Radius();
            g.ell.ratio = 1.0;
            g.t0 = f;
            g.t1 = l;
            g.occt_param = true;
            g.eval = new Geom_Circle(ax, ci->Radius());
        } else if (auto el = Handle(Geom_Ellipse)::DownCast(base)) {
            const gp_Ax2 ax = el->Position();
            g.k = EdgeGeom::K::Ellipse;
            g.ell.center = ax.Location();
            g.ell.normal = ax.Direction();
            g.ell.major = gp_Vec(ax.XDirection()) * el->MajorRadius();
            g.ell.ratio = el->MinorRadius() / el->MajorRadius();
            g.t0 = f;
            g.t1 = l;
            g.occt_param = true;
            g.eval = new Geom_Ellipse(ax, el->MajorRadius(), el->MinorRadius());
        } else {
            g.k = EdgeGeom::K::Spline;
            Handle(Geom_BSplineCurve) b;
            try {
                if (auto bs = Handle(Geom_BSplineCurve)::DownCast(base)) {
                    b = Handle(Geom_BSplineCurve)::DownCast(bs->Copy());
                    g.occt_param = true;
                } else if (auto bz = Handle(Geom_BezierCurve)::DownCast(base)) {
                    b = GeomConvert::CurveToBSplineCurve(bz);
                    g.occt_param = true;
                } else {
                    b = GeomConvert::CurveToBSplineCurve(new Geom_TrimmedCurve(base, f, l));
                    g.occt_param = false;
                }
                if (b.IsNull()) fail(where + ": no B-spline form for " + type_name(base));
                clamp_span(b, g.occt_param ? f : b->FirstParameter(),
                           g.occt_param ? l : b->LastParameter(), where.c_str());
            } catch (const Standard_Failure& e) {
                fail(where + ": cannot convert " + type_name(base) + " to an exact B-spline: " +
                     e.GetMessageString());
            }
            g.bs = to_bs3(b, tight(), where.c_str());
            g.eval = rebuild(g.bs);
            g.t0 = g.eval->FirstParameter();
            g.t1 = g.eval->LastParameter();
            if (g.occt_param && (std::fabs(g.t0 - f) > Precision::PConfusion() ||
                                 std::fabs(g.t1 - l) > Precision::PConfusion()))
                g.occt_param = false;
        }
        if (recognized) g.occt_param = false;  // parameters are the analytic curve's
        return g;
    }

    int vertex_for(const TopoDS_Vertex& V, int edge_rec) {
        if (const int* r = vertex_rec_.Seek(V)) return *r;
        const int vr = push(VertexR{});
        const int pr = push(PointR{BRep_Tool::Pnt(V)});
        at<VertexR>(vr).edge = edge_rec;
        at<VertexR>(vr).point = pr;
        vertex_rec_.Bind(V, vr);
        return vr;
    }

    int edge_for(const TopoDS_Edge& E) {
        if (const int* i = edge_ix_.Seek(E)) return *i;
        const std::string where = "edge " + std::to_string(edges_.size()) + " (face " + std::to_string(face_no_) + ")";
        EdgeInfo info;
        info.edge = TopoDS::Edge(E.Oriented(TopAbs_FORWARD));
        info.g = build_edge(info.edge, where);
        info.rec = push(EdgeR{});
        TopoDS_Vertex V0, V1;
        TopExp::Vertices(info.edge, V0, V1);
        if (V0.IsNull() || V1.IsNull()) fail(where + " has no vertices");
        // Written parameters must reproduce the vertices.
        const double t[2] = {info.g.t0, info.g.t1};
        const TopoDS_Vertex* vs[2] = {&V0, &V1};
        for (int k = 0; k < 2; ++k) {
            const gp_Pnt P = BRep_Tool::Pnt(*vs[k]);
            const double d = info.g.eval->Value(t[k]).Distance(P);
            const double tol = std::max(tight(), BRep_Tool::Tolerance(*vs[k]));
            if (d > tol)
                fail(where + ": curve at written parameter " + num(t[k]) + " is " + num(d) +
                     " from its " + (k == 0 ? "start" : "end") + " vertex (tolerance " + num(tol) + ")");
        }
        int curve = -1;
        switch (info.g.k) {
        case EdgeGeom::K::Line: curve = push(info.g.line); break;
        case EdgeGeom::K::Ellipse: curve = push(info.g.ell); break;
        case EdgeGeom::K::Spline: curve = push(IntcurveR{info.g.bs}); break;
        }
        EdgeR& er = at<EdgeR>(info.rec);
        er.curve = curve;
        er.t0 = info.g.t0;
        er.t1 = info.g.t1;
        const int v0 = vertex_for(V0, info.rec);
        const int v1 = vertex_for(V1, info.rec);
        at<EdgeR>(info.rec).v0 = v0;
        at<EdgeR>(info.rec).v1 = v1;
        edges_.push_back(std::move(info));
        const int ix = static_cast<int>(edges_.size()) - 1;
        edge_ix_.Bind(E, ix);
        return ix;
    }

    // ---- faces -----------------------------------------------------------------------

    int add_face(const TopoDS_Face& F, bool dbl, int shell_rec) {
        const std::string where = "face " + std::to_string(face_no_);
        const int frec = push(FaceR{});
        SurfGeom g = build_surface(F);
        int srec = -1;
        switch (g.k) {
        case SurfGeom::K::Plane: srec = push(g.plane); break;
        case SurfGeom::K::Cone: srec = push(g.cone); break;
        case SurfGeom::K::Sphere: srec = push(g.sphere); break;
        case SurfGeom::K::Torus: srec = push(g.torus); break;
        case SurfGeom::K::Spline:
            splines_.push_back(g.bs);
            srec = push(SplineR{static_cast<int>(splines_.size()) - 1});
            break;
        }
        {
            FaceR& fr = at<FaceR>(frec);
            fr.shell = shell_rec;
            fr.surface = srec;
            fr.dbl = dbl;
        }
        at<FaceR>(frec).reversed = face_reversed(F, g, where);

        // Wires with orientation composed through the face; the outer wire first.
        std::vector<TopoDS_Wire> wires;
        const TopoDS_Wire outer = BRepTools::OuterWire(F);
        for (TopExp_Explorer xw(F, TopAbs_WIRE); xw.More(); xw.Next()) {
            const TopoDS_Wire w = TopoDS::Wire(xw.Current());
            if (!outer.IsNull() && w.IsSame(outer)) wires.insert(wires.begin(), w);
            else wires.push_back(w);
        }
        // Seams of periodic analytic faces (cylinder, cone, sphere, torus) are not written: ACIS
        // bounds such a face by its remaining closed loops (a cylinder band by its two
        // circles, a whole sphere or torus by none), as AutoCAD writes them. A seam stays when
        // dropping it would leave an open chain or when another face also uses the edge.
        const bool periodic_analytic = g.k != SurfGeom::K::Plane && g.k != SurfGeom::K::Spline;
        auto only_here = [&](const TopoDS_Edge& E) {
            const TopTools_ListOfShape* faces = edge_faces_.Seek(E);
            if (faces == nullptr) return false;
            for (const TopoDS_Shape& other : *faces)
                if (!other.IsSame(F)) return false;
            return true;
        };
        struct Co { int rec; int edge_ix; TopoDS_Edge e; };
        std::vector<Co> face_coedges;
        int prev_loop = -1;
        for (const TopoDS_Wire& W : wires) {
            std::vector<TopoDS_Edge> es;
            for (BRepTools_WireExplorer we(W, F); we.More(); we.Next()) {
                const TopoDS_Edge& E = we.Current();
                if (BRep_Tool::Degenerated(E)) continue;
                const TopAbs_Orientation o = E.Orientation();
                if (o != TopAbs_FORWARD && o != TopAbs_REVERSED)
                    fail(where + ": internal/external edge inside a face cannot be written");
                es.push_back(E);
            }
            if (es.empty()) continue;
            // Loop closure: each coedge ends where the next one starts.
            const size_t n = es.size();
            for (size_t i = 0; i < n; ++i)
                if (!TopExp::LastVertex(es[i], true).IsSame(TopExp::FirstVertex(es[(i + 1) % n], true)))
                    fail(where + ": wire is not a closed chain of edges");
            std::vector<std::vector<TopoDS_Edge>> chains{es};
            if (periodic_analytic) {
                std::vector<bool> seam(n);
                size_t first_seam = n;
                for (size_t i = 0; i < n; ++i) {
                    seam[i] = BRep_Tool::IsClosed(es[i], F) && only_here(es[i]);
                    if (seam[i] && first_seam == n) first_seam = i;
                }
                if (first_seam < n) {
                    std::vector<std::vector<TopoDS_Edge>> split;
                    std::vector<TopoDS_Edge> cur;
                    for (size_t k = 1; k <= n; ++k) {  // ends on first_seam, which flushes
                        const size_t i = (first_seam + k) % n;
                        if (!seam[i]) {
                            cur.push_back(es[i]);
                        } else if (!cur.empty()) {
                            split.push_back(std::move(cur));
                            cur.clear();
                        }
                    }
                    bool closed = true;
                    for (const auto& c : split)
                        closed &= TopExp::LastVertex(c.back(), true).IsSame(TopExp::FirstVertex(c.front(), true));
                    if (closed) chains = std::move(split);
                }
            }
            for (const auto& chain : chains) {
                std::vector<Co> cos;
                for (const TopoDS_Edge& E : chain) {
                    const int ix = edge_for(E);
                    const int crec = push(CoedgeR{});
                    CoedgeR& cr = at<CoedgeR>(crec);
                    cr.edge = edges_[ix].rec;
                    cr.reversed = (E.Orientation() == TopAbs_REVERSED);
                    edges_[ix].coedges.push_back(crec);
                    if (at<EdgeR>(edges_[ix].rec).coedge < 0) at<EdgeR>(edges_[ix].rec).coedge = crec;
                    cos.push_back({crec, ix, E});
                }
                const int lrec = push(LoopR{});
                at<LoopR>(lrec).face = frec;
                at<LoopR>(lrec).coedge = cos.front().rec;
                if (prev_loop < 0) at<FaceR>(frec).loop = lrec;
                else at<LoopR>(prev_loop).next = lrec;
                prev_loop = lrec;
                const size_t m = cos.size();
                for (size_t i = 0; i < m; ++i) {
                    CoedgeR& cr = at<CoedgeR>(cos[i].rec);
                    cr.loop = lrec;
                    cr.next = cos[(i + 1) % m].rec;
                    cr.prev = cos[(i + m - 1) % m].rec;
                }
                face_coedges.insert(face_coedges.end(), cos.begin(), cos.end());
            }
        }
        if (g.k == SurfGeom::K::Spline) {
            const int sid = std::get<SplineR>(recs_[srec]).surf;
            for (const Co& c : face_coedges) {
                PcurveR pc;
                pc.surf = sid;
                pc.bs = make_pcurve(c.e, F, edges_[c.edge_ix].g, g, pc.fitol, where);
                if (c.e.Orientation() == TopAbs_REVERSED) pc.bs = negated(pc.bs);
                const int prec = push(std::move(pc));
                at<CoedgeR>(c.rec).pcurve = prec;
            }
        }
        ++face_no_;
        return frec;
    }

    // ---- pcurves ---------------------------------------------------------------------

    // Exact B-spline copy of an OCCT pcurve over [f, l] in its own parameters, or null.
    static Handle(Geom2d_BSplineCurve) exact_bs2(Handle(Geom2d_Curve) pc, double f, double l) {
        while (auto t = Handle(Geom2d_TrimmedCurve)::DownCast(pc)) pc = t->BasisCurve();
        try {
            if (auto ln = Handle(Geom2d_Line)::DownCast(pc)) {
                TColgp_Array1OfPnt2d P(1, 2);
                P(1) = ln->Value(f);
                P(2) = ln->Value(l);
                TColStd_Array1OfReal K(1, 2);
                TColStd_Array1OfInteger M(1, 2);
                K(1) = f;
                K(2) = l;
                M(1) = M(2) = 2;
                return new Geom2d_BSplineCurve(P, K, M, 1);
            }
            Handle(Geom2d_BSplineCurve) b;
            if (auto bs = Handle(Geom2d_BSplineCurve)::DownCast(pc))
                b = Handle(Geom2d_BSplineCurve)::DownCast(bs->Copy());
            else if (auto bz = Handle(Geom2d_BezierCurve)::DownCast(pc))
                b = Geom2dConvert::CurveToBSplineCurve(bz);
            else
                return nullptr;
            clamp_span(b, f, l, "pcurve");
            if (std::fabs(b->FirstParameter() - f) > Precision::PConfusion() ||
                std::fabs(b->LastParameter() - l) > Precision::PConfusion())
                return nullptr;
            return b;
        } catch (const std::exception&) {
            return nullptr;
        } catch (const Standard_Failure&) {
            return nullptr;
        }
    }

    // Largest distance between the written surface along the pcurve and the written 3D curve.
    static double pcurve_error(const Handle(Geom2d_BSplineCurve)& b2, const SurfGeom& g,
                               const EdgeGeom& eg, int samples) {
        double err = 0.0;
        for (int i = 0; i <= samples; ++i) {
            const double t = eg.t0 + (eg.t1 - eg.t0) * i / samples;
            const gp_Pnt2d uv = b2->Value(t);
            err = std::max(err, g.eval->Value(uv.X(), uv.Y()).Distance(eg.eval->Value(t)));
        }
        return err;
    }

    BsCurve2 make_pcurve(const TopoDS_Edge& E, const TopoDS_Face& F, const EdgeGeom& eg,
                         const SurfGeom& g, double& fitol, const std::string& where) {
        double pf = 0, pl = 0;
        const Handle(Geom2d_Curve) pc = BRep_Tool::CurveOnSurface(E, F, pf, pl);
        const bool same = BRep_Tool::SameParameter(E) && BRep_Tool::SameRange(E);
        const double accept = std::max({2.0 * BRep_Tool::Tolerance(E), 1e-6, tight()});
        const double uv_tol = 1e-12 * std::max({std::fabs(g.u2 - g.u1), std::fabs(g.v2 - g.v1), 1.0});
        auto mapped = [&](Handle(Geom2d_BSplineCurve) b) {
            if (g.flip_u || g.flip_v)
                for (int i = 1; i <= b->NbPoles(); ++i) b->SetPole(i, flip(g, b->Pole(i)));
            return b;
        };
        // Error of the pcurve as written (rebuilt from the ACIS arrays).
        auto written = [&](const Handle(Geom2d_BSplineCurve)& b, BsCurve2& out, int samples) {
            out = to_bs2(b, uv_tol, where.c_str());
            return pcurve_error(rebuild(out), g, eg, samples);
        };
        BsCurve2 out;
        // Exact: both parameterisations are OCCT's, so the OCCT pcurve is the ACIS pcurve.
        if (!pc.IsNull() && same && eg.occt_param && g.occt_param) {
            if (Handle(Geom2d_BSplineCurve) b = exact_bs2(pc, eg.t0, eg.t1); !b.IsNull()) {
                b = mapped(b);
                const double err = written(b, out, 64);
                if (err <= std::max(tight(), BRep_Tool::Tolerance(E))) {
                    fitol = err <= tight() ? 0.0 : err;
                    return out;
                }
            }
        }
        // Approximation in the coedge parameter: piecewise cubic Hermite through points of the
        // written 3D curve located in the written surface's parameters (seeded by the OCCT
        // pcurve so seam sides stay right). Segments join C0, so a kink of the parameter
        // mapping at a surface knot only costs local bisection.
        GeomAPI_ProjectPointOnCurve on_occt;
        auto seed = [&](double t) -> gp_Pnt2d {
            const gp_Pnt P = eg.eval->Value(t);
            if (pc.IsNull()) {
                GeomAPI_ProjectPointOnSurf proj(P, g.eval);
                if (proj.NbPoints() == 0) fail(where + ": cannot locate an edge point on the spline surface");
                double u, v;
                proj.LowerDistanceParameters(u, v);
                return gp_Pnt2d(u, v);
            }
            double s = eg.f + (eg.l - eg.f) * (t - eg.t0) / (eg.t1 - eg.t0);
            if (eg.occt_param) {
                s = t;
            } else {
                on_occt.Init(P, eg.occt, eg.f, eg.l);
                double best = 1e300, pick = s;
                for (int k = 1; k <= on_occt.NbPoints(); ++k)
                    if (std::fabs(on_occt.Parameter(k) - s) < best) {
                        best = std::fabs(on_occt.Parameter(k) - s);
                        pick = on_occt.Parameter(k);
                    }
                s = pick;
            }
            const double sp = same ? s : pf + (s - eg.f) / (eg.l - eg.f) * (pl - pf);
            const gp_Pnt2d occt_uv = pc->Value(sp);
            return g.occt_param ? locate_on_spline(g, P, flip(g, occt_uv)) : written_uv(g, occt_uv, P);
        };
        auto at = [&](double t, const gp_Pnt2d& guess) { return locate_on_spline(g, eg.eval->Value(t), guess); };
        // One-sided derivative of uv(t) towards t + step (second-order finite difference).
        auto deriv = [&](double t, const gp_Pnt2d& uv, double step) {
            const double d = 1e-4 * step;
            const gp_Pnt2d a = at(t + d, uv), b = at(t + 2 * d, a);
            return (uv.XY() * -3.0 + a.XY() * 4.0 - b.XY()) / (2 * d);
        };
        auto bezier = [](const std::array<gp_Pnt2d, 4>& P, double s) {
            const double r = 1 - s;
            return gp_Pnt2d(P[0].XY() * (r * r * r) + P[1].XY() * (3 * r * r * s) +
                            P[2].XY() * (3 * r * s * s) + P[3].XY() * (s * s * s));
        };
        // A located point off the surface means the edge does not lie on the written surface:
        // no subdivision can fix that, so it is reported instead of refined.
        auto on_surface = [&](double t, const gp_Pnt2d& uv) {
            const double d = g.eval->Value(uv.X(), uv.Y()).Distance(eg.eval->Value(t));
            if (d > accept)
                fail(where + ": edge lies " + num(d) + " off its spline surface (tolerance " + num(accept) + ")");
            return uv;
        };
        std::vector<std::array<gp_Pnt2d, 4>> segs;
        std::vector<double> ends;
        int calls = 0;
        auto segment = [&](auto&& self, double ta, gp_Pnt2d ua, double tb, gp_Pnt2d ub, int depth) -> void {
            if (++calls > 100000) fail(where + ": pcurve needs more than 100000 segments");
            const double h = tb - ta;
            const gp_XY da = deriv(ta, ua, h), db = deriv(tb, ub, -h);
            const std::array<gp_Pnt2d, 4> P{ua, gp_Pnt2d(ua.XY() + da * (h / 3)),
                                            gp_Pnt2d(ub.XY() - db * (h / 3)), ub};
            double err = 0.0;
            for (double s : {0.25, 0.5, 0.75}) {
                const gp_Pnt2d q = bezier(P, s);
                err = std::max(err, g.eval->Value(q.X(), q.Y()).Distance(eg.eval->Value(ta + s * h)));
            }
            if (err > 0.25 * accept && depth < 40) {
                const double tm = ta + 0.5 * h;
                const gp_Pnt2d um = on_surface(tm, at(tm, bezier(P, 0.5)));
                self(self, ta, ua, tm, um, depth + 1);
                self(self, tm, um, tb, ub, depth + 1);
                return;
            }
            segs.push_back(P);
            ends.push_back(tb);
        };
        constexpr int kStart = 8;
        std::vector<gp_Pnt2d> uv0;
        for (int i = 0; i <= kStart; ++i) {
            const double t = i == kStart ? eg.t1 : eg.t0 + (eg.t1 - eg.t0) * i / kStart;
            uv0.push_back(on_surface(t, seed(t)));
        }
        for (int i = 0; i < kStart; ++i)
            segment(segment, eg.t0 + (eg.t1 - eg.t0) * i / kStart, uv0[i],
                    i + 1 == kStart ? eg.t1 : eg.t0 + (eg.t1 - eg.t0) * (i + 1) / kStart, uv0[i + 1], 0);
        const int ns = static_cast<int>(segs.size());
        TColgp_Array1OfPnt2d P(1, 3 * ns + 1);
        TColStd_Array1OfReal K(1, ns + 1);
        TColStd_Array1OfInteger M(1, ns + 1);
        K(1) = eg.t0;
        M(1) = 4;
        P(1) = segs.front()[0];
        for (int i = 0; i < ns; ++i) {
            for (int k = 1; k <= 3; ++k) P(3 * i + 1 + k) = segs[i][k];
            K(i + 2) = ends[i];
            M(i + 2) = i + 1 == ns ? 4 : 3;
        }
        Handle(Geom2d_BSplineCurve) b;
        try {
            b = new Geom2d_BSplineCurve(P, K, M, 3);
        } catch (const Standard_Failure& e) {
            fail(where + ": pcurve construction failed: " + e.GetMessageString());
        }
        const double err = written(b, out, 16 * ns);
        if (err > accept)
            fail(where + ": no pcurve within " + num(accept) + " of the edge on its spline surface (best " +
                 num(err) + ")");
        fitol = err <= tight() ? 0.0 : err;
        return out;
    }

    // ---- output ----------------------------------------------------------------------

    std::string serialize(int n_bodies) {
        Ser s(opt_.version);
        for (int i = 0; i < static_cast<int>(recs_.size()); ++i) {
            std::visit([&](const auto& r) { put(s, r); }, recs_[i]);
        }
        std::string body = s.take();

        char date[64];
        const std::time_t now = std::time(nullptr);
        std::tm tmv{};
#ifdef _WIN32
        gmtime_s(&tmv, &now);
#else
        gmtime_r(&now, &tmv);
#endif
        std::strftime(date, sizeof(date), "%a %b %d %H:%M:%S %Y", &tmv);
        const std::string product = "Chudware";
        const std::string acis = opt_.version == 400 ? "ACIS 4.00 NT" : "ACIS 7.00 NT";
        std::string out;
        // Record count: ODA writes it at 4.00; 7.0 producers write 0 (end marker required).
        out += std::to_string(opt_.version) + " " +
               std::to_string(opt_.version == 400 ? recs_.size() : 0) + " " +
               std::to_string(n_bodies) + " 0 \n";
        out += std::to_string(product.size()) + " " + product + " " + std::to_string(acis.size()) +
               " " + acis + " " + std::to_string(std::strlen(date)) + " " + date + " \n";
        out += num(opt_.mm_per_unit) + " 9.9999999999999995e-007 1e-010 \n";
        out += body;
        out += "End-of-ACIS-data\n";
        return out;
    }

    static void put(Ser& s, const BodyR& r) {
        s.begin("body");
        s.ptr(r.lump);
        s.ptr(-1);  // wire
        s.ptr(-1);  // transform
        s.end();
    }
    static void put(Ser& s, const LumpR& r) {
        s.begin("lump");
        s.ptr(r.next);
        s.ptr(r.shell);
        s.ptr(r.body);
        s.end();
    }
    static void put(Ser& s, const ShellR& r) {
        s.begin("shell");
        s.ptr(r.next);
        s.ptr(-1);  // subshell
        s.ptr(r.face);
        s.ptr(-1);  // wire
        s.ptr(r.lump);
        s.end();
    }
    static void put(Ser& s, const FaceR& r) {
        s.begin("face");
        s.ptr(r.next);
        s.ptr(r.loop);
        s.ptr(r.shell);
        s.ptr(-1);  // subshell
        s.ptr(r.surface);
        s.tok(r.reversed ? "reversed" : "forward");
        if (r.dbl) {
            s.tok("double");
            s.tok("out");
        } else {
            s.tok("single");
        }
        s.end();
    }
    static void put(Ser& s, const LoopR& r) {
        s.begin("loop");
        s.ptr(r.next);
        s.ptr(r.coedge);
        s.ptr(r.face);
        s.end();
    }
    static void put(Ser& s, const CoedgeR& r) {
        s.begin("coedge");
        s.ptr(r.next);
        s.ptr(r.prev);
        s.ptr(r.partner);
        s.ptr(r.edge);
        s.tok(r.reversed ? "reversed" : "forward");
        s.ptr(r.loop);
        s.ptr(r.pcurve);
        s.end();
    }
    static void put(Ser& s, const EdgeR& r) {
        s.begin("edge");
        s.ptr(r.v0);
        if (s.version() >= 700) s.real(r.t0);
        s.ptr(r.v1);
        if (s.version() >= 700) s.real(r.t1);
        s.ptr(r.coedge);
        s.ptr(r.curve);
        s.tok("forward");
        if (s.version() >= 700) {
            s.tok("@7");
            s.tok("unknown");
        }
        s.end();
    }
    static void put(Ser& s, const VertexR& r) {
        s.begin("vertex");
        s.ptr(r.edge);
        s.ptr(r.point);
        s.end();
    }
    static void put(Ser& s, const PointR& r) {
        s.begin("point");
        s.xyz(r.p.XYZ());
        s.end();
    }
    static void put(Ser& s, const StraightR& r) {
        s.begin("straight-curve");
        s.xyz(r.root.XYZ());
        s.xyz(r.dir.XYZ());
        s.tok("I");
        s.tok("I");
        s.end();
    }
    static void put(Ser& s, const EllipseR& r) {
        s.begin("ellipse-curve");
        s.xyz(r.center.XYZ());
        s.xyz(r.normal.XYZ());
        s.xyz(r.major.XYZ());
        s.real(r.ratio);
        s.tok("I");
        s.tok("I");
        s.end();
    }
    static void put(Ser& s, const IntcurveR& r) {
        s.begin("intcurve-curve");
        s.tok("forward");
        s.tok("{");
        s.next_subtype();
        s.tok("exactcur");
        if (s.version() > 400) s.tok("full");
        put_bs3(s, r.bs);
        s.real(0.0);  // fit tolerance: the B-spline is the curve
        s.nl();
        for (const char* t : {"null_surface", "null_surface", "nullbs", "nullbs", "I", "I"}) s.tok(t);
        put_disc(s, r.bs.kv, r.bs.deg);
        for (const char* t : {"F", "1", "F", "0", "}", "I", "I"}) s.tok(t);
        s.end();
    }
    static void put(Ser& s, const PlaneR& r) {
        s.begin("plane-surface");
        s.xyz(r.root.XYZ());
        s.xyz(r.normal.XYZ());
        s.xyz(r.udir.XYZ());
        s.tok("forward_v");
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.end();
    }
    static void put(Ser& s, const ConeR& r) {
        s.begin("cone-surface");
        s.xyz(r.center.XYZ());
        s.xyz(r.axis.XYZ());
        s.xyz(r.major.XYZ());
        s.real(1.0);  // circular base
        s.tok("I");
        s.tok("I");
        s.real(r.sine);
        s.real(r.cosine);
        s.real(r.scale);
        s.tok("forward");
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.end();
    }
    static void put(Ser& s, const SphereR& r) {
        s.begin("sphere-surface");
        s.xyz(r.center.XYZ());
        s.real(r.radius);
        s.xyz(r.udir.XYZ());
        s.xyz(r.pole.XYZ());
        s.tok("forward_v");
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.end();
    }
    static void put(Ser& s, const TorusR& r) {
        s.begin("torus-surface");
        s.xyz(r.center.XYZ());
        s.xyz(r.axis.XYZ());
        s.real(r.major);
        s.real(r.minor);
        s.xyz(r.udir.XYZ());
        s.tok("forward_v");
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.end();
    }
    void put(Ser& s, const SplineR& r) const {
        s.begin("spline-surface");
        s.tok("forward");
        put_spline_subtype(s, r.surf, splines_[r.surf]);
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.end();
    }
    void put(Ser& s, const PcurveR& r) const {
        s.begin("pcurve");
        s.integer(0);  // explicit pcurve, not a reference into an intcurve
        s.tok("forward");
        s.tok("{");
        s.next_subtype();  // the par_cur is numbered before the surface nested in it
        s.tok("exppc");
        put_bs2(s, r.bs);
        s.real(r.fitol);
        s.nl();
        s.tok("spline");
        s.tok("forward");
        put_spline_subtype(s, r.surf, splines_[r.surf]);
        for (int i = 0; i < 4; ++i) s.tok("I");
        s.tok("}");
        s.real(0.0);
        s.real(0.0);
        s.end();
    }
};

}  // namespace

std::string write_sat_bodies(const std::vector<TopoDS_Shape>& bodies, const SatWriteOptions& opt) {
    if (opt.version != 400 && opt.version != 700)
        fail("unsupported SAT version " + std::to_string(opt.version) + " (400 or 700)");
    if (!std::isfinite(opt.mm_per_unit) || opt.mm_per_unit <= 0.0)
        fail("mm_per_unit must be positive, got " + num(opt.mm_per_unit));
    Writer w(opt);
    return w.run(bodies);
}

std::string write_sat(const TopoDS_Shape& shape, const SatWriteOptions& opt) {
    return write_sat_bodies(std::vector<TopoDS_Shape>{shape}, opt);
}

std::vector<std::pair<int, std::string>> encrypt_dxf_sat(const std::string& sat) {
    std::vector<std::pair<int, std::string>> out;
    size_t pos = 0;
    int line_no = 0;
    while (pos < sat.size()) {
        size_t eol = sat.find('\n', pos);
        if (eol == std::string::npos) eol = sat.size();
        std::string_view line(sat.data() + pos, eol - pos);
        pos = eol + 1;
        ++line_no;
        if (!line.empty() && line.back() == '\r') line.remove_suffix(1);
        std::string enc;
        enc.reserve(line.size() + 8);
        for (const char ch : line) {
            const auto c = static_cast<unsigned char>(ch);
            if (c == ' ') {
                enc += ' ';
            } else if (c > 32 && c < 127) {
                enc += static_cast<char>(159 - c);
                if (c == 'A') enc += ' ';  // 'A' encrypts to '^', which DXF escapes as "^ "
            } else {
                char hex[8];
                std::snprintf(hex, sizeof(hex), "0x%02X", c);
                fail("SAT line " + std::to_string(line_no) + " holds byte " + hex +
                     "; only printable ASCII can be encrypted for DXF");
            }
        }
        if (enc.empty()) {
            out.emplace_back(1, std::string());
            continue;
        }
        size_t at = 0;
        bool first = true;
        while (at < enc.size()) {
            size_t n = std::min<size_t>(255, enc.size() - at);
            if (at + n < enc.size() && enc[at + n - 1] == '^') --n;  // keep "^ " together
            out.emplace_back(first ? 1 : 3, enc.substr(at, n));
            first = false;
            at += n;
        }
    }
    return out;
}

}  // namespace chudware::doc
