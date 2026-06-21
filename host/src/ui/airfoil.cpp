// Airfoil profile generator — see airfoil.h. The NACA closed-form maths follow Abbott & von
// Doenhoff, "Theory of Wing Sections"; the 5-digit reflex mean line follows the XFOIL naca.f
// reference. Geometry is assembled in unit-chord space, then chord-scaled and AoA-rotated.

#include "airfoil.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>

namespace ui::airfoil {

namespace {

constexpr double kPi = 3.14159265358979323846;

// Cosine spacing on [0,1]: x_i = (1 - cos(pi * i/(n-1)))/2. Clusters samples near the leading
// edge (x->0) and trailing edge (x->1), where the curvature is highest.
std::vector<double> cosine_x(int n) {
    std::vector<double> xs;
    if (n < 2) n = 2;
    xs.reserve(n);
    for (int i = 0; i < n; ++i)
        xs.push_back(0.5 * (1.0 - std::cos(kPi * double(i) / double(n - 1))));
    return xs;
}

// NACA 4-digit half-thickness y_t(x) for unit chord. `closedTE` swaps the trailing 4th-order
// coefficient so y_t(1) == 0 exactly (a sharp, closed trailing edge).
double thickness(double x, double t, bool closedTE) {
    const double a4 = closedTE ? -0.1036 : -0.1015;
    return 5.0 * t * (0.2969 * std::sqrt(std::max(x, 0.0)) - 0.1260 * x - 0.3516 * x * x +
                      0.2843 * x * x * x + a4 * x * x * x * x);
}

// Combine a camber line (yc, dyc/dx) with the half-thickness into upper/lower surface points,
// scale to `chord`, rotate by the angle of attack about the leading edge, and append.
void emit_surfaces(const std::vector<double>& xs, double t, bool closedTE,
                   double chord, double aoaDeg, Profile& out,
                   double (*camber)(double, void*), double (*dcamber)(double, void*),
                   void* ctx) {
    const double a = -aoaDeg * kPi / 180.0;     // CW rotation -> positive AoA = nose up vs +X
    const double ca = std::cos(a), sa = std::sin(a);
    auto place = [&](double xc, double yc) -> std::array<double, 2> {
        double X = xc * chord, Y = yc * chord;          // unit-chord -> mm
        return {{X * ca - Y * sa, X * sa + Y * ca}};    // rotate about LE (origin)
    };
    out.upper.reserve(xs.size());
    out.lower.reserve(xs.size());
    for (double x : xs) {
        double yt = thickness(x, t, closedTE);
        double yc = camber ? camber(x, ctx) : 0.0;
        double dy = dcamber ? dcamber(x, ctx) : 0.0;
        double th = std::atan(dy);
        double st = std::sin(th), ct = std::cos(th);
        out.upper.push_back(place(x - yt * st, yc + yt * ct));
        out.lower.push_back(place(x + yt * st, yc - yt * ct));
    }
    // Closed TE only when both the thickness AND the camber vanish there (camber lines reach
    // yc(1)=0 for 4-digit; the 5-digit standard line also does). Compare the surface endpoints.
    const auto& u = out.upper.back();
    const auto& l = out.lower.back();
    out.sharpTE = std::hypot(u[0] - l[0], u[1] - l[1]) < 1e-6 * std::max(1.0, chord);
}

// Trim/skip a leading name line and parse every "x y" pair in the stream.
std::vector<std::array<double, 2>> parse_pairs(std::istream& in) {
    std::vector<std::array<double, 2>> pts;
    std::string line;
    while (std::getline(in, line)) {
        std::istringstream ls(line);
        double x, y;
        if (ls >> x >> y) pts.push_back({{x, y}});
    }
    return pts;
}

}  // namespace

Profile naca4(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE) {
    Profile p;
    std::string d;
    for (char c : code) if (std::isdigit((unsigned char)c)) d.push_back(c);
    if (d.size() != 4) { p.error = "NACA 4-digit code must be 4 digits (e.g. 2412)"; return p; }
    double m = (d[0] - '0') / 100.0;            // max camber
    double pp = (d[1] - '0') / 10.0;            // camber position
    double t = ((d[2] - '0') * 10 + (d[3] - '0')) / 100.0;  // thickness
    if (t <= 0.0) { p.error = "NACA 4-digit thickness must be > 0"; return p; }
    p.name = "NACA " + d;

    // Camber closures captured by value through a small context struct.
    struct Ctx { double m, p; } ctx{m, pp};
    auto yc = [](double x, void* v) -> double {
        const Ctx& c = *static_cast<Ctx*>(v);
        if (c.m <= 0.0 || c.p <= 0.0) return 0.0;
        if (x < c.p) return c.m / (c.p * c.p) * (2.0 * c.p * x - x * x);
        return c.m / ((1.0 - c.p) * (1.0 - c.p)) * ((1.0 - 2.0 * c.p) + 2.0 * c.p * x - x * x);
    };
    auto dyc = [](double x, void* v) -> double {
        const Ctx& c = *static_cast<Ctx*>(v);
        if (c.m <= 0.0 || c.p <= 0.0) return 0.0;
        if (x < c.p) return 2.0 * c.m / (c.p * c.p) * (c.p - x);
        return 2.0 * c.m / ((1.0 - c.p) * (1.0 - c.p)) * (c.p - x);
    };
    emit_surfaces(cosine_x(nPts), t, closedTE, chord, aoaDeg, p, yc, dyc, &ctx);
    return p;
}

Profile naca5(const std::string& code, double chord, double aoaDeg, int nPts, bool closedTE) {
    Profile p;
    std::string d;
    for (char c : code) if (std::isdigit((unsigned char)c)) d.push_back(c);
    if (d.size() != 5) { p.error = "NACA 5-digit code must be 5 digits (e.g. 23012)"; return p; }
    int Lc = d[0] - '0';            // design-Cl code
    int Pc = d[1] - '0';            // camber-position code (1..5)
    int Qc = d[2] - '0';            // 0 = standard, 1 = reflex
    double t = ((d[3] - '0') * 10 + (d[4] - '0')) / 100.0;
    if (t <= 0.0) { p.error = "NACA 5-digit thickness must be > 0"; return p; }
    if (Pc < 1 || Pc > 5) { p.error = "NACA 5-digit camber position (2nd digit) must be 1..5"; return p; }
    double clDesign = Lc * 0.15;    // L*(3/2)/10
    p.name = "NACA " + d;

    // Documented 5-station table (for Cl=0.3): m (the camber-line parameter r) and k1.
    // Index by Pc-1 -> camber position 0.05..0.25 chord.
    static const double mTab[5]  = {0.0580, 0.1260, 0.2025, 0.2900, 0.3910};
    static const double k1Tab[5] = {361.400, 51.640, 15.957, 6.643, 3.230};
    double m = mTab[Pc - 1];
    double k1 = k1Tab[Pc - 1] * (clDesign / 0.3);   // scale to the actual design Cl
    bool reflex = (Qc == 1);
    // Reflex ratio k2/k1 (XFOIL naca.f): closes the reflex tail to zero pitching moment.
    double k2k1 = 0.0;
    if (reflex) {
        double xf = 0.05 * Pc;   // camber position for this table row
        k2k1 = (3.0 * (m - xf) * (m - xf) - m * m * m) / std::pow(1.0 - m, 3.0);
    }

    struct Ctx { double m, k1, k2k1; bool reflex; } ctx{m, k1, k2k1, reflex};
    auto yc = [](double x, void* v) -> double {
        const Ctx& c = *static_cast<Ctx*>(v);
        double mm = c.m, k1 = c.k1;
        if (!c.reflex) {
            if (x < mm) return (k1 / 6.0) * (x * x * x - 3.0 * mm * x * x + mm * mm * (3.0 - mm) * x);
            return (k1 / 6.0) * mm * mm * mm * (1.0 - x);
        }
        double r = c.k2k1, om3 = std::pow(1.0 - mm, 3.0);
        if (x < mm)
            return (k1 / 6.0) * (std::pow(x - mm, 3.0) - r * om3 * x - mm * mm * mm * x + mm * mm * mm);
        return (k1 / 6.0) * (r * std::pow(x - mm, 3.0) - r * om3 * x - mm * mm * mm * x + mm * mm * mm);
    };
    auto dyc = [](double x, void* v) -> double {
        const Ctx& c = *static_cast<Ctx*>(v);
        double mm = c.m, k1 = c.k1;
        if (!c.reflex) {
            if (x < mm) return (k1 / 6.0) * (3.0 * x * x - 6.0 * mm * x + mm * mm * (3.0 - mm));
            return -(k1 / 6.0) * mm * mm * mm;
        }
        double r = c.k2k1, om3 = std::pow(1.0 - mm, 3.0);
        if (x < mm) return (k1 / 6.0) * (3.0 * std::pow(x - mm, 2.0) - r * om3 - mm * mm * mm);
        return (k1 / 6.0) * (3.0 * r * std::pow(x - mm, 2.0) - r * om3 - mm * mm * mm);
    };
    emit_surfaces(cosine_x(nPts), t, closedTE, chord, aoaDeg, p, yc, dyc, &ctx);
    return p;
}

Profile fromDatFile(const std::string& path, double chord, double aoaDeg) {
    Profile p;
    std::ifstream f(path);
    if (!f) { p.error = "could not open " + path; return p; }
    // Title = first line (kept as the profile name if it isn't itself a coordinate pair).
    std::string first;
    std::getline(f, first);
    {
        std::istringstream ts(first);
        double a, b;
        if (!(ts >> a >> b)) {
            // Trim the title for the label.
            size_t s = first.find_first_not_of(" \t\r\n");
            size_t e = first.find_last_not_of(" \t\r\n");
            if (s != std::string::npos) p.name = first.substr(s, e - s + 1);
        } else {
            f.clear();
            f.seekg(0);  // first line was data -> reparse from the top
        }
    }
    if (p.name.empty()) {
        size_t slash = path.find_last_of("/\\");
        p.name = slash == std::string::npos ? path : path.substr(slash + 1);
    }

    std::vector<std::array<double, 2>> raw = parse_pairs(f);
    if (raw.size() < 4) { p.error = "no usable coordinates in " + path; return p; }

    std::vector<std::array<double, 2>> upper, lower;  // unit-chord, LE -> TE
    // Lednicer files lead with a count pair like "61. 61." (both values > 1, i.e. not real
    // x/y in [0,1]). Selig files start straight into coordinates near (1, 0).
    bool lednicer = raw.front()[0] > 1.5 && raw.front()[1] > 1.5;
    if (lednicer) {
        int nu = (int)std::lround(raw.front()[0]);
        int nl = (int)std::lround(raw.front()[1]);
        if (nu < 2 || nl < 2 || (size_t)(nu + nl) + 1 > raw.size()) {
            p.error = "malformed Lednicer header in " + path; return p;
        }
        for (int i = 0; i < nu; ++i) upper.push_back(raw[1 + i]);          // LE -> TE
        for (int i = 0; i < nl; ++i) lower.push_back(raw[1 + nu + i]);     // LE -> TE
    } else {
        // Selig: TE -> (over the top) -> LE -> (along the bottom) -> TE. Split at the global
        // minimum-x point (the leading edge), then orient both halves LE -> TE.
        size_t le = 0;
        for (size_t i = 1; i < raw.size(); ++i)
            if (raw[i][0] < raw[le][0]) le = i;
        for (size_t i = 0; i <= le; ++i) upper.push_back(raw[le - i]);     // reverse -> LE..TE
        for (size_t i = le; i < raw.size(); ++i) lower.push_back(raw[i]);  // LE..TE
    }
    if (upper.size() < 2 || lower.size() < 2) { p.error = "could not split surfaces in " + path; return p; }

    // Scale + rotate (about the leading edge = upper.front(), pre-transform).
    const double a = -aoaDeg * kPi / 180.0;
    const double ca = std::cos(a), sa = std::sin(a);
    std::array<double, 2> le = upper.front();
    auto xform = [&](const std::array<double, 2>& q) -> std::array<double, 2> {
        double X = (q[0] - le[0]) * chord, Y = (q[1] - le[1]) * chord;
        return {{X * ca - Y * sa, X * sa + Y * ca}};
    };
    for (auto& q : upper) q = xform(q);
    for (auto& q : lower) q = xform(q);
    p.sharpTE = std::hypot(upper.back()[0] - lower.back()[0],
                           upper.back()[1] - lower.back()[1]) < 1e-6 * std::max(1.0, chord);
    p.upper = std::move(upper);
    p.lower = std::move(lower);
    return p;
}

}  // namespace ui::airfoil
