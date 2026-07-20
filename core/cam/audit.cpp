// core/cam/audit.cpp — deterministic G-code modal parser + aicad.cam_audit/1 checks.
#define _USE_MATH_DEFINES
#include "cam/audit.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <limits>
#include <set>
#include <sstream>
#include <unordered_set>

#include "cam/feeds.h"

namespace aicad {
namespace cam {
namespace {

struct ModalState {
    int motion = 0;          // 0=G0, 1=G1, 2=G2, 3=G3
    int plane = 17;          // G17/18/19
    int units = 21;          // G20/G21
    int distance = 90;       // G90/G91
    int work_offset = 54;
    int work_offset_sub = 0;
    int tool = 0;
    double feed = 0;         // mm/min
    double rpm = 0;
    int spindle = 0;         // 0=off, 3=CW, 4=CCW
    Vec3 pos{0, 0, 0};
    bool have_pos = false;
};

struct Bounds {
    bool any = false;
    double xmin = 0, xmax = 0, ymin = 0, ymax = 0, zmin = 0, zmax = 0;
    void add(const Vec3& p) {
        if (!any) {
            xmin = xmax = p.x; ymin = ymax = p.y; zmin = zmax = p.z;
            any = true;
            return;
        }
        xmin = std::min(xmin, p.x); xmax = std::max(xmax, p.x);
        ymin = std::min(ymin, p.y); ymax = std::max(ymax, p.y);
        zmin = std::min(zmin, p.z); zmax = std::max(zmax, p.z);
    }
};

// Strip comments (; ... EOL or ( ... )) and uppercase the remainder.
std::string strip_comments(const std::string& line) {
    std::string out;
    out.reserve(line.size());
    bool in_paren = false;
    for (size_t i = 0; i < line.size(); ++i) {
        char c = line[i];
        if (in_paren) {
            if (c == ')') in_paren = false;
            continue;
        }
        if (c == '(') { in_paren = true; continue; }
        if (c == ';' || c == '%') break;
        out.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
    }
    return out;
}

// Parse a word value starting at s[i] after the letter. Advances i past the number.
bool parse_number(const std::string& s, size_t& i, double& out) {
    while (i < s.size() && s[i] == ' ') ++i;
    size_t start = i;
    if (i < s.size() && (s[i] == '+' || s[i] == '-')) ++i;
    bool digits = false;
    while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) { digits = true; ++i; }
    if (i < s.size() && s[i] == '.') {
        ++i;
        while (i < s.size() && std::isdigit(static_cast<unsigned char>(s[i]))) { digits = true; ++i; }
    }
    if (!digits) return false;
    try {
        out = std::stod(s.substr(start, i - start));
    } catch (...) {
        return false;
    }
    return true;
}

struct Word {
    char letter = 0;
    double value = 0;
};

std::vector<Word> tokenize(const std::string& line) {
    std::vector<Word> words;
    size_t i = 0;
    while (i < line.size()) {
        if (std::isspace(static_cast<unsigned char>(line[i]))) { ++i; continue; }
        char L = line[i];
        if (!std::isalpha(static_cast<unsigned char>(L))) { ++i; continue; }
        ++i;
        double v = 0;
        if (!parse_number(line, i, v)) continue;
        words.push_back(Word{L, v});
    }
    return words;
}

// Shared rate math with stock_sim::move_time_s (feed-limited; accel ignored here
// so audit matches the plan's "same constants" rule for G0/G1-G3/G4/M6).
double segment_time_s(bool rapid, const Vec3& from, const Vec3& to, double feed,
                      const Machine& m) {
    double rapid_xy = m.rapid_xy > 0 ? m.rapid_xy : 5000;
    double rapid_z = m.rapid_z > 0 ? m.rapid_z : 3000;
    if (rapid) {
        double dxy = std::hypot(to.x - from.x, to.y - from.y);
        double dz = std::fabs(to.z - from.z);
        return 60.0 * std::max(dxy / rapid_xy, dz / rapid_z);
    }
    double len = std::sqrt((to.x - from.x) * (to.x - from.x) +
                           (to.y - from.y) * (to.y - from.y) +
                           (to.z - from.z) * (to.z - from.z));
    double feed_mm_min = feed > 0 ? feed : 600;
    return 60.0 * len / feed_mm_min;
}

void stock_box(const Stock& st, Vec3& lo, Vec3& hi) {
    if (st.type == StockType::Cylinder || st.type == StockType::CylinderOffset) {
        lo = {st.base.x - st.radius, st.base.y - st.radius, st.base.z};
        hi = {st.base.x + st.radius, st.base.y + st.radius, st.base.z + st.height};
        return;
    }
    // Box / BoxOffset / FromSolid: use min/max (FromSolid may also fill solid bbox;
    // session jobs under test use explicit Box).
    lo = st.min;
    hi = st.max;
    if (st.type == StockType::FromSolid && !st.solid.verts.empty()) {
        lo = st.solid.verts[0];
        hi = st.solid.verts[0];
        for (const auto& v : st.solid.verts) {
            lo.x = std::min(lo.x, v.x); lo.y = std::min(lo.y, v.y); lo.z = std::min(lo.z, v.z);
            hi.x = std::max(hi.x, v.x); hi.y = std::max(hi.y, v.y); hi.z = std::max(hi.z, v.z);
        }
    }
}

double toolpath_tool_radius(const Job& job, const std::vector<Toolpath>& tps) {
    double r = 0;
    for (const auto& tp : tps) {
        const Tool* t = job.tools.find(tp.tool_number);
        if (t) r = std::max(r, t->effective_radius());
    }
    return r;
}

void toolpath_bbox(const std::vector<Toolpath>& tps, Vec3& lo, Vec3& hi, bool& any) {
    any = false;
    for (const auto& tp : tps) {
        if (tp.moves.empty() && tp.bbox_min.x > tp.bbox_max.x) continue;
        Vec3 a = tp.bbox_min, b = tp.bbox_max;
        // Prefer live move extents when bbox stats look uninitialized.
        if (!(a.x <= b.x && a.y <= b.y && a.z <= b.z) ||
            (a.x == 0 && a.y == 0 && a.z == 0 && b.x == 0 && b.y == 0 && b.z == 0 && !tp.moves.empty())) {
            a = {1e300, 1e300, 1e300};
            b = {-1e300, -1e300, -1e300};
            bool got = false;
            for (const auto& mv : tp.moves) {
                a.x = std::min(a.x, mv.to.x); a.y = std::min(a.y, mv.to.y); a.z = std::min(a.z, mv.to.z);
                b.x = std::max(b.x, mv.to.x); b.y = std::max(b.y, mv.to.y); b.z = std::max(b.z, mv.to.z);
                got = true;
            }
            if (!got) continue;
        }
        if (!any) { lo = a; hi = b; any = true; }
        else {
            lo.x = std::min(lo.x, a.x); lo.y = std::min(lo.y, a.y); lo.z = std::min(lo.z, a.z);
            hi.x = std::max(hi.x, b.x); hi.y = std::max(hi.y, b.y); hi.z = std::max(hi.z, b.z);
        }
    }
}

double ref_chipload(const MaterialData& mat, double dia_mm) {
    double scale = std::max(0.4, std::min(3.0, dia_mm / std::max(1e-6, mat.fz_ref_dia)));
    return mat.fz_ref * scale;
}

}  // namespace

json audit_program(const Job& job,
                   const std::string& gcode,
                   const std::string& dialect,
                   const Machine& profile,
                   const std::vector<Toolpath>& toolpaths,
                   const std::string& source,
                   const std::string& machine_profile_id) {
    std::vector<std::string> errors, warnings;
    ModalState st;
    Bounds cut_bounds;   // G1/G2/G3 only (round-trip)
    Bounds all_bounds;   // any positioned move
    std::set<int> tools_used;
    int tool_changes = 0;
    int last_tool = -1;
    double est_time = 0;
    double feed_lo = std::numeric_limits<double>::infinity();
    double feed_hi = 0;
    double rpm_lo = std::numeric_limits<double>::infinity();
    double rpm_hi = 0;
    bool saw_feed = false, saw_rpm = false;
    int line_count = 0;

    // Stock / envelope from first setup (multi-setup posts still share one envelope check).
    const Setup* setup = job.setups.empty() ? nullptr : &job.setups.front();
    Vec3 stock_lo{0, 0, 0}, stock_hi{0, 0, 0};
    if (setup) stock_box(setup->stock, stock_lo, stock_hi);
    double stock_top = setup ? stock_hi.z : 0;
    // Travel envelope: stock expanded by travel, and absolute travel from origin.
    Vec3 travel = profile.travel;
    if (travel.x <= 0) travel.x = 1000;
    if (travel.y <= 0) travel.y = 1000;
    if (travel.z <= 0) travel.z = 1000;

    std::unordered_set<int> known_tools;
    for (const auto& t : job.tools.tools) known_tools.insert(t.number);

    std::string material_id = setup ? setup->stock.material : std::string("aluminum");
    const MaterialData* mat = CuttingDB::builtin().find(material_id);
    if (!mat) mat = CuttingDB::builtin().find("aluminum");

    std::istringstream iss(gcode);
    std::string raw;
    while (std::getline(iss, raw)) {
        ++line_count;
        std::string line = strip_comments(raw);
        if (line.empty()) continue;
        auto words = tokenize(line);
        if (words.empty()) continue;

        bool has_xyz = false;
        double X = st.pos.x, Y = st.pos.y, Z = st.pos.z;
        double I = 0, J = 0, R = 0;
        bool has_I = false, has_J = false, has_R = false;
        double dwell = 0;
        bool has_dwell = false;
        bool m6 = false;
        int t_word = -1;

        for (const Word& w : words) {
            int gi = static_cast<int>(std::lround(w.value));
            // G54.1 Pn arrives as G54.1 — value 54.1
            if (w.letter == 'G') {
                double gv = w.value;
                int g = static_cast<int>(std::floor(gv + 1e-9));
                double frac = gv - g;
                if (g == 0 || g == 1 || g == 2 || g == 3) {
                    st.motion = g;
                } else if (g == 4) {
                    // dwell — P may follow
                } else if (g == 17 || g == 18 || g == 19) {
                    st.plane = g;
                } else if (g == 20) {
                    st.units = 20;
                    errors.push_back("G20 inch mode is not supported (jobs are mm)");
                } else if (g == 21) {
                    st.units = 21;
                } else if (g == 90) {
                    st.distance = 90;
                } else if (g == 91) {
                    st.distance = 91;
                } else if (g >= 54 && g <= 59) {
                    st.work_offset = g;
                    if (frac > 0.05 && frac < 0.2) {
                        // G54.1 — look for P
                        st.work_offset_sub = 0;
                    }
                } else if (g == 28 || g == 30 || g == 40 || g == 41 || g == 42 ||
                           g == 43 || g == 49 || g == 53 || g == 80 || g == 81 ||
                           g == 82 || g == 83 || g == 84 || g == 85 || g == 98 ||
                           g == 99 || g == 94 || g == 95 || g == 64 || g == 61 ||
                           g == 10 || g == 50 || g == 92 || g == 96 || g == 97) {
                    // known, ignored for audit modal subset
                } else {
                    char buf[64];
                    std::snprintf(buf, sizeof(buf), "unknown G-code G%g", gv);
                    warnings.push_back(buf);
                }
                if (g == 4) {
                    // dwell handled via P below
                    has_dwell = true;
                }
                continue;
            }
            if (w.letter == 'M') {
                int m = gi;
                if (m == 3) st.spindle = 3;
                else if (m == 4) st.spindle = 4;
                else if (m == 5) st.spindle = 0;
                else if (m == 6) m6 = true;
                else if (m == 0 || m == 1 || m == 2 || m == 30 || m == 7 || m == 8 ||
                         m == 9 || m == 48 || m == 49 || m == 8 || m == 7) {
                    // known
                } else {
                    char buf[64];
                    std::snprintf(buf, sizeof(buf), "unknown M-code M%d", m);
                    warnings.push_back(buf);
                }
                continue;
            }
            if (w.letter == 'T') {
                t_word = gi;
                st.tool = gi;
                tools_used.insert(gi);
                continue;
            }
            if (w.letter == 'S') {
                st.rpm = w.value;
                saw_rpm = true;
                rpm_lo = std::min(rpm_lo, w.value);
                rpm_hi = std::max(rpm_hi, w.value);
                if (profile.min_rpm > 0 && w.value + 1e-9 < profile.min_rpm) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "spindle S%.0f below machine min_rpm %.0f", w.value, profile.min_rpm);
                    errors.push_back(buf);
                }
                if (profile.max_rpm > 0 && w.value - 1e-9 > profile.max_rpm) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "spindle S%.0f exceeds machine max_rpm %.0f", w.value, profile.max_rpm);
                    errors.push_back(buf);
                }
                continue;
            }
            if (w.letter == 'F') {
                st.feed = w.value;
                saw_feed = true;
                feed_lo = std::min(feed_lo, w.value);
                feed_hi = std::max(feed_hi, w.value);
                if (w.value <= 0) {
                    errors.push_back("feed F must be > 0");
                } else if (profile.max_feed > 0 && w.value - 1e-9 > profile.max_feed) {
                    char buf[128];
                    std::snprintf(buf, sizeof(buf),
                                  "feed F%.3f exceeds machine max_feed %.3f mm/min",
                                  w.value, profile.max_feed);
                    errors.push_back(buf);
                }
                // chipload check deferred until we have tool flutes + S
                continue;
            }
            if (w.letter == 'X') { X = w.value; has_xyz = true; continue; }
            if (w.letter == 'Y') { Y = w.value; has_xyz = true; continue; }
            if (w.letter == 'Z') { Z = w.value; has_xyz = true; continue; }
            if (w.letter == 'I') { I = w.value; has_I = true; continue; }
            if (w.letter == 'J') { J = w.value; has_J = true; continue; }
            if (w.letter == 'R') { R = w.value; has_R = true; continue; }
            if (w.letter == 'P') {
                if (has_dwell || st.motion == 4) {
                    dwell = w.value;
                    has_dwell = true;
                } else if (st.work_offset >= 54 && st.work_offset <= 59) {
                    st.work_offset_sub = gi;
                }
                continue;
            }
            // Other letters (N, H, D, L, Q, A, B, C, …) ignored.
            (void)has_I; (void)has_J; (void)has_R; (void)R; (void)I; (void)J;
        }

        if (t_word >= 0) {
            if (known_tools.find(t_word) == known_tools.end()) {
                char buf[96];
                std::snprintf(buf, sizeof(buf), "unknown tool T%d not in job tool library", t_word);
                errors.push_back(buf);
            }
        }
        if (m6) {
            ++tool_changes;
            if (st.tool != last_tool) last_tool = st.tool;
            est_time += profile.tool_change_s;
        }

        // Chipload warning when both F and S are known and a tool is selected.
        if (saw_feed && saw_rpm && st.feed > 0 && st.rpm > 0 && st.tool > 0 && mat) {
            const Tool* tl = job.tools.find(st.tool);
            if (tl && tl->flutes > 0) {
                double fz = st.feed / (st.rpm * std::max(1, tl->flutes));
                double ref = ref_chipload(*mat, std::max(0.1, tl->diameter));
                double lo = 0.25 * ref;
                double hi = 4.0 * ref;
                if (fz < lo || fz > hi) {
                    char buf[160];
                    std::snprintf(buf, sizeof(buf),
                                  "chipload %.4f mm/tooth outside CuttingDB band [%.4f, %.4f] for %s",
                                  fz, lo, hi, material_id.c_str());
                    // de-dupe: only warn once per distinct message roughly — push always; callers tolerate.
                    if (warnings.empty() || warnings.back() != buf)
                        warnings.push_back(buf);
                }
            }
        }

        if (has_dwell && dwell > 0) {
            // G4 P — seconds (common) or ms on some Fanuc; treat as seconds when < 1000
            // matching our post's dwell_seconds path for grbl/linuxcnc.
            est_time += dwell;
        }

        if (!has_xyz) continue;

        Vec3 target = st.pos;
        if (st.distance == 90) {
            // absolute: only overwrite axes present
            for (const Word& w : words) {
                if (w.letter == 'X') target.x = w.value;
                if (w.letter == 'Y') target.y = w.value;
                if (w.letter == 'Z') target.z = w.value;
            }
        } else {
            for (const Word& w : words) {
                if (w.letter == 'X') target.x += w.value;
                if (w.letter == 'Y') target.y += w.value;
                if (w.letter == 'Z') target.z += w.value;
            }
        }

        Vec3 from = st.have_pos ? st.pos : target;
        bool rapid = (st.motion == 0);
        bool cut = (st.motion == 1 || st.motion == 2 || st.motion == 3);

        if (st.have_pos) {
            est_time += segment_time_s(rapid, from, target, st.feed, profile);
        }

        // Envelope checks on target.
        all_bounds.add(target);
        if (cut) cut_bounds.add(target);

        // Within machine travel (from origin-ish envelope).
        if (std::fabs(target.x) - 1e-6 > travel.x ||
            std::fabs(target.y) - 1e-6 > travel.y ||
            target.z - 1e-6 > travel.z + std::max(0.0, stock_top) ||
            target.z + 1e-6 < -travel.z) {
            // Soft: also allow stock-expanded envelope. Only error if outside both.
            bool outside_stock_travel =
                target.x + 1e-6 < stock_lo.x - travel.x ||
                target.x - 1e-6 > stock_hi.x + travel.x ||
                target.y + 1e-6 < stock_lo.y - travel.y ||
                target.y - 1e-6 > stock_hi.y + travel.y ||
                target.z + 1e-6 < stock_lo.z - travel.z ||
                target.z - 1e-6 > stock_hi.z + travel.z;
            if (outside_stock_travel) {
                char buf[160];
                std::snprintf(buf, sizeof(buf),
                              "position (%.3f,%.3f,%.3f) outside machine envelope",
                              target.x, target.y, target.z);
                errors.push_back(buf);
            }
        }

        // Z min ≥ stock min Z − 1 mm
        if (setup && target.z + 1e-6 < stock_lo.z - 1.0) {
            char buf[128];
            std::snprintf(buf, sizeof(buf),
                          "Z %.3f below stock min Z − 1 mm (stock_min_z=%.3f)",
                          target.z, stock_lo.z);
            errors.push_back(buf);
        }

        // Rapids below clearance while XY inside stock box → warning
        if (rapid && setup) {
            bool xy_in = target.x >= stock_lo.x - 1e-6 && target.x <= stock_hi.x + 1e-6 &&
                         target.y >= stock_lo.y - 1e-6 && target.y <= stock_hi.y + 1e-6;
            if (xy_in && target.z + 1e-6 < stock_top) {
                char buf[128];
                std::snprintf(buf, sizeof(buf),
                              "G0 below stock top Z=%.3f while XY inside stock (Z=%.3f)",
                              stock_top, target.z);
                warnings.push_back(buf);
            }
        }

        st.pos = target;
        st.have_pos = true;
    }

    // Round-trip: cut extents vs toolpath bbox expanded by 0.01 + tool radius.
    Vec3 tp_lo, tp_hi;
    bool tp_any = false;
    toolpath_bbox(toolpaths, tp_lo, tp_hi, tp_any);
    if (tp_any && cut_bounds.any) {
        double rad = toolpath_tool_radius(job, toolpaths);
        double pad = 0.01 + rad;
        auto outside = [&](double v, double lo, double hi) {
            return v + 1e-6 < lo - pad || v - 1e-6 > hi + pad;
        };
        if (outside(cut_bounds.xmin, tp_lo.x, tp_hi.x) ||
            outside(cut_bounds.xmax, tp_lo.x, tp_hi.x) ||
            outside(cut_bounds.ymin, tp_lo.y, tp_hi.y) ||
            outside(cut_bounds.ymax, tp_lo.y, tp_hi.y) ||
            outside(cut_bounds.zmin, tp_lo.z, tp_hi.z) ||
            outside(cut_bounds.zmax, tp_lo.z, tp_hi.z)) {
            errors.push_back(
                "round-trip: G1/G2/G3 extents exceed toolpath bbox + 0.01mm + tool radius");
        }
    }

    // Deduplicate warnings/errors lightly (preserve order).
    auto uniq = [](std::vector<std::string>& v) {
        std::vector<std::string> out;
        std::unordered_set<std::string> seen;
        for (auto& s : v) {
            if (seen.insert(s).second) out.push_back(std::move(s));
        }
        v.swap(out);
    };
    uniq(errors);
    uniq(warnings);

    json tools_arr = json::array();
    for (int t : tools_used) tools_arr.push_back(t);

    json bounds = json::object();
    if (all_bounds.any) {
        bounds = json{
            {"x", json::array({all_bounds.xmin, all_bounds.xmax})},
            {"y", json::array({all_bounds.ymin, all_bounds.ymax})},
            {"z", json::array({all_bounds.zmin, all_bounds.zmax})},
        };
    } else {
        bounds = json{
            {"x", json::array({0.0, 0.0})},
            {"y", json::array({0.0, 0.0})},
            {"z", json::array({0.0, 0.0})},
        };
    }

    json feed_range = json::array({
        saw_feed ? feed_lo : 0.0,
        saw_feed ? feed_hi : 0.0,
    });
    json rpm_range = json::array({
        saw_rpm ? rpm_lo : 0.0,
        saw_rpm ? rpm_hi : 0.0,
    });

    std::string mid = machine_profile_id;
    if (mid.empty()) mid = "benchtop_vmc";

    return json{
        {"schema", "aicad.cam_audit/1"},
        {"ok", errors.empty()},
        {"errors", errors},
        {"warnings", warnings},
        {"stats", json{
            {"lines", line_count},
            {"tool_changes", tool_changes},
            {"tools_used", tools_arr},
            {"bounds", bounds},
            {"feed_range_mm_min", feed_range},
            {"rpm_range", rpm_range},
            {"est_time_s", est_time},
        }},
        {"provenance", json{
            {"dialect", dialect},
            {"machine_profile", mid},
            {"source", source},
        }},
    };
}

}  // namespace cam
}  // namespace aicad
