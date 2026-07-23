// core/geom/airfoil.cpp — see airfoil.h. NACA closed-form maths follow Abbott & von Doenhoff;
// the 5-digit reflex mean line follows the XFOIL naca.f reference. Ported from
// host/src/ui/airfoil.cpp (kept byte-identical in the maths so the agent's aerofoil matches the
// sketcher's Airfoil tool).
#include "geom/airfoil.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <fstream>
#include <sstream>

namespace chudware {
namespace geom {

namespace {

constexpr double kPi = 3.14159265358979323846;

std::vector<double> cosine_x(int n) {
    std::vector<double> xs;
    if (n < 2) n = 2;
    xs.reserve(n);
    for (int i = 0; i < n; ++i)
        xs.push_back(0.5 * (1.0 - std::cos(kPi * double(i) / double(n - 1))));
    return xs;
}

double thickness(double x, double t, bool closedTE) {
    const double a4 = closedTE ? -0.1036 : -0.1015;
    return 5.0 * t * (0.2969 * std::sqrt(std::max(x, 0.0)) - 0.1260 * x - 0.3516 * x * x +
                      0.2843 * x * x * x + a4 * x * x * x * x);
}

void emit_surfaces(const std::vector<double>& xs, double t, bool closedTE, double chord,
                   double aoaDeg, AirfoilProfile& out, double (*camber)(double, void*),
                   double (*dcamber)(double, void*), void* ctx) {
    const double a = -aoaDeg * kPi / 180.0;
    const double ca = std::cos(a), sa = std::sin(a);
    auto place = [&](double xc, double yc) -> std::array<double, 2> {
        double X = xc * chord, Y = yc * chord;
        return {{X * ca - Y * sa, X * sa + Y * ca}};
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
    const auto& u = out.upper.back();
    const auto& l = out.lower.back();
    out.sharpTE = std::hypot(u[0] - l[0], u[1] - l[1]) < 1e-6 * std::max(1.0, chord);
}

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

AirfoilProfile naca4(const std::string& code, double chord, double aoaDeg, int nPts,
                     bool closedTE) {
    AirfoilProfile p;
    std::string d;
    for (char c : code) if (std::isdigit((unsigned char)c)) d.push_back(c);
    if (d.size() != 4) { p.error = "NACA 4-digit code must be 4 digits (e.g. 2412)"; return p; }
    double m = (d[0] - '0') / 100.0;
    double pp = (d[1] - '0') / 10.0;
    double t = ((d[2] - '0') * 10 + (d[3] - '0')) / 100.0;
    if (t <= 0.0) { p.error = "NACA 4-digit thickness must be > 0"; return p; }
    p.name = "NACA " + d;

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

AirfoilProfile naca5(const std::string& code, double chord, double aoaDeg, int nPts,
                     bool closedTE) {
    AirfoilProfile p;
    std::string d;
    for (char c : code) if (std::isdigit((unsigned char)c)) d.push_back(c);
    if (d.size() != 5) { p.error = "NACA 5-digit code must be 5 digits (e.g. 23012)"; return p; }
    int Lc = d[0] - '0';
    int Pc = d[1] - '0';
    int Qc = d[2] - '0';
    double t = ((d[3] - '0') * 10 + (d[4] - '0')) / 100.0;
    if (t <= 0.0) { p.error = "NACA 5-digit thickness must be > 0"; return p; }
    if (Pc < 1 || Pc > 5) { p.error = "NACA 5-digit camber position (2nd digit) must be 1..5"; return p; }
    double clDesign = Lc * 0.15;
    p.name = "NACA " + d;

    static const double mTab[5]  = {0.0580, 0.1260, 0.2025, 0.2900, 0.3910};
    static const double k1Tab[5] = {361.400, 51.640, 15.957, 6.643, 3.230};
    double m = mTab[Pc - 1];
    double k1 = k1Tab[Pc - 1] * (clDesign / 0.3);
    bool reflex = (Qc == 1);
    double k2k1 = 0.0;
    if (reflex) {
        double xf = 0.05 * Pc;
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

AirfoilProfile from_dat_file(const std::string& path, double chord, double aoaDeg) {
    AirfoilProfile p;
    std::ifstream f(path);
    if (!f) { p.error = "could not open " + path; return p; }
    std::string first;
    std::getline(f, first);
    {
        std::istringstream ts(first);
        double a, b;
        if (!(ts >> a >> b)) {
            size_t s = first.find_first_not_of(" \t\r\n");
            size_t e = first.find_last_not_of(" \t\r\n");
            if (s != std::string::npos) p.name = first.substr(s, e - s + 1);
        } else {
            f.clear();
            f.seekg(0);
        }
    }
    if (p.name.empty()) {
        size_t slash = path.find_last_of("/\\");
        p.name = slash == std::string::npos ? path : path.substr(slash + 1);
    }

    std::vector<std::array<double, 2>> raw = parse_pairs(f);
    if (raw.size() < 4) { p.error = "no usable coordinates in " + path; return p; }

    std::vector<std::array<double, 2>> upper, lower;
    bool lednicer = raw.front()[0] > 1.5 && raw.front()[1] > 1.5;
    if (lednicer) {
        int nu = (int)std::lround(raw.front()[0]);
        int nl = (int)std::lround(raw.front()[1]);
        if (nu < 2 || nl < 2 || (size_t)(nu + nl) + 1 > raw.size()) {
            p.error = "malformed Lednicer header in " + path; return p;
        }
        for (int i = 0; i < nu; ++i) upper.push_back(raw[1 + i]);
        for (int i = 0; i < nl; ++i) lower.push_back(raw[1 + nu + i]);
    } else {
        size_t le = 0;
        for (size_t i = 1; i < raw.size(); ++i)
            if (raw[i][0] < raw[le][0]) le = i;
        for (size_t i = 0; i <= le; ++i) upper.push_back(raw[le - i]);
        for (size_t i = le; i < raw.size(); ++i) lower.push_back(raw[i]);
    }
    if (upper.size() < 2 || lower.size() < 2) { p.error = "could not split surfaces in " + path; return p; }

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

AirfoilProfile airfoil_profile(const std::string& naca, const std::string& dat, double chord,
                               double aoaDeg, int nPts, bool closedTE) {
    if (!dat.empty()) return from_dat_file(dat, chord, aoaDeg);
    std::string d;
    for (char c : naca) if (std::isdigit((unsigned char)c)) d.push_back(c);
    if (d.size() == 4) return naca4(naca, chord, aoaDeg, nPts, closedTE);
    if (d.size() == 5) return naca5(naca, chord, aoaDeg, nPts, closedTE);
    AirfoilProfile p;
    p.error = "airfoil needs a 4- or 5-digit NACA code (e.g. \"2412\") or a 'dat' file path";
    return p;
}

}  // namespace geom
}  // namespace chudware
