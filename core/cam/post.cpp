// core/cam/post.cpp — the post-processor: PostConfig dialect presets + the NC
// generator that renders toolpaths into controller G-code, plus the setup sheet.
// Dialect tables are clean-room from public G-code references; no vendor posts copied.
#include "cam/post.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <sstream>
#include <unordered_map>

#include "cam/feeds.h"

namespace chudware {
namespace cam {

// ───────────────────────────────────────────────────── number formatting ──
static std::string fmt_num(double v, const NumFmt& f) {
    if (std::fabs(v) < 5e-9) v = 0.0;  // kill -0
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.*f", f.decimals, v);
    std::string s(buf);
    if (!f.trailing_zeros && s.find('.') != std::string::npos) {
        size_t last = s.find_last_not_of('0');
        if (s[last] == '.') --last;
        s.erase(last + 1);
    }
    // leading zero handling: ".5" vs "0.5"
    if (!f.leading_zero) {
        if (s.rfind("0.", 0) == 0) s.erase(0, 1);
        else if (s.rfind("-0.", 0) == 0) s.erase(1, 1);
    }
    if (f.plus_sign && v >= 0) s = "+" + s;
    return s;
}

// ───────────────────────────────────────────────────── PostConfig JSON ──
json PostConfig::to_json() const {
    return json{{"id", id}, {"name", name}, {"description", description},
                {"program_numbers", program_numbers}, {"sequence_numbers", sequence_numbers},
                {"percent_wrap", percent_wrap}, {"arcs_supported", arcs_supported},
                {"arcs_ijk", arcs_ijk}, {"r_for_arcs", r_for_arcs}, {"canned_drill", canned_drill},
                {"work_offset", work_offset}, {"comp_supported", comp_supported},
                {"laser_mode", laser_mode}, {"lathe", lathe}, {"diameter_mode", diameter_mode},
                {"program_stop", program_stop}, {"tool_change", tool_change}};
}
PostConfig PostConfig::from_json(const json& j) {
    PostConfig c;  // overrides applied field-by-field (only those present)
    auto S = [&](const char* k, std::string& d) { if (j.contains(k) && j[k].is_string()) d = j[k].get<std::string>(); };
    auto B = [&](const char* k, bool& d) { if (j.contains(k) && j[k].is_boolean()) d = j[k].get<bool>(); };
    S("id", c.id); S("name", c.name); S("description", c.description);
    B("program_numbers", c.program_numbers); B("sequence_numbers", c.sequence_numbers);
    B("percent_wrap", c.percent_wrap); B("arcs_supported", c.arcs_supported);
    B("arcs_ijk", c.arcs_ijk); B("r_for_arcs", c.r_for_arcs); B("canned_drill", c.canned_drill);
    B("work_offset", c.work_offset); B("comp_supported", c.comp_supported);
    B("laser_mode", c.laser_mode); B("lathe", c.lathe); B("diameter_mode", c.diameter_mode);
    S("program_stop", c.program_stop); S("tool_change", c.tool_change);
    S("css_on", c.css_on); S("css_off", c.css_off); S("css_clamp", c.css_clamp);
    S("spindle_cw", c.spindle_cw); S("spindle_ccw", c.spindle_ccw); S("spindle_off", c.spindle_off);
    S("coolant_flood", c.coolant_flood); S("coolant_mist", c.coolant_mist); S("coolant_off", c.coolant_off);
    S("comment_open", c.comment_open); S("comment_close", c.comment_close);
    return c;
}

// ───────────────────────────────────────────────────── dialect presets ──
namespace {
PostConfig base_mill(const char* id, const char* name, const char* desc) {
    PostConfig c;
    c.id = id; c.name = name; c.description = desc;
    return c;
}

std::vector<PostConfig> build_dialects() {
    std::vector<PostConfig> v;

    // GRBL — hobby CNC/router. No tool changer, no work-offset macro chains, R-less
    // arcs use IJK, no canned cycles (expand to long-hand), comments in ( ).
    {
        PostConfig c = base_mill("grbl", "GRBL", "Grbl 1.1 router/mill (no ATC, no canned cycles)");
        c.program_numbers = false; c.sequence_numbers = false; c.percent_wrap = false;
        c.tool_change = "";          // single tool; emit M6 only if multi-tool (handled below)
        c.length_offset = false;
        c.canned_drill = false;      // grbl has no G81/G83 -> long-hand
        c.work_offset = true;
        c.arcs_ijk = true; c.r_for_arcs = false;
        c.coolant_flood = "M8"; c.coolant_mist = "M7"; c.coolant_off = "M9";
        v.push_back(c);
    }
    // LinuxCNC / EMC2 — full canned cycles, IJK arcs, G43, % wrap, O-word optional.
    {
        PostConfig c = base_mill("linuxcnc", "LinuxCNC", "LinuxCNC / EMC2 (full canned cycles)");
        c.percent_wrap = true; c.program_numbers = false; c.sequence_numbers = false;
        c.tool_change = "M6"; c.length_offset = true; c.canned_drill = true;
        c.dwell_seconds = true; c.dwell_word = "P";
        v.push_back(c);
    }
    // Fanuc — industrial standard: O-number, N-lines, %, G43 H, canned cycles, IJK.
    {
        PostConfig c = base_mill("fanuc", "Fanuc", "Fanuc 0i/30i (O-number, N-lines, canned cycles)");
        c.program_numbers = true; c.sequence_numbers = true; c.percent_wrap = true;
        c.tool_change = "M6"; c.tool_word_before_change = true; c.length_offset = true;
        c.canned_drill = true; c.dwell_seconds = false; c.dwell_word = "P";  // P in ms
        v.push_back(c);
    }
    // Haas — Fanuc-like with Haas niceties (G53 retract handled by program_begin).
    {
        PostConfig c = base_mill("haas", "Haas", "Haas NGC (O-number, N-lines, canned cycles)");
        c.program_numbers = true; c.sequence_numbers = true; c.percent_wrap = true;
        c.tool_change = "M6"; c.length_offset = true; c.canned_drill = true;
        c.program_begin = "G0 G17 G40 G49 G80 G90";
        v.push_back(c);
    }
    // Mach3 / Mach4 — like LinuxCNC but no %, N optional, canned cycles supported.
    {
        PostConfig c = base_mill("mach3", "Mach3/Mach4", "ArtSoft Mach3/Mach4 (canned cycles, no %)");
        c.percent_wrap = false; c.program_numbers = false; c.sequence_numbers = false;
        c.tool_change = "M6"; c.length_offset = true; c.canned_drill = true;
        v.push_back(c);
    }
    // Marlin — FFF/laser/diode + hobby router on Marlin firmware. Laser mode: S=power,
    // M3/M5 gate the beam; no canned cycles; mm; G0/G1 only; no work offsets.
    {
        PostConfig c = base_mill("marlin", "Marlin", "Marlin laser/diode + router (G0/G1, laser power S)");
        c.percent_wrap = false; c.program_numbers = false; c.sequence_numbers = false;
        c.tool_change = ""; c.length_offset = false; c.canned_drill = false;
        c.work_offset = false; c.arcs_supported = true; c.arcs_ijk = true;
        c.laser_mode = true; c.torch_on = "M3"; c.torch_off = "M5"; c.power_max = 255;
        c.comment_open = ";"; c.comment_close = "";   // Marlin line comments
        c.spindle_off = "M5"; c.program_stop = "M2";
        v.push_back(c);
    }
    // Siemens 840D / ShopMill — ISO-mode subset: %, N, T="..." D1, M6, CYCLE81/83 macros
    // are richer; we emit ISO-dialect G-code (G-code mode), G54, IJK arcs.
    {
        PostConfig c = base_mill("siemens", "Siemens 840D", "Sinumerik 840D ISO mode (G-code dialect)");
        c.percent_wrap = true; c.program_numbers = false; c.sequence_numbers = true;
        c.tool_change = "M6"; c.tool_word_before_change = true; c.length_offset = true;
        c.length_offset_code = "G43"; c.canned_drill = true;
        c.comment_open = ";"; c.comment_close = "";
        v.push_back(c);
    }
    // Centroid / Tormach PathPilot (LinuxCNC-derived) — canned cycles, %, G43, IJK.
    {
        PostConfig c = base_mill("centroid", "Centroid/Tormach", "Centroid / Tormach PathPilot (LinuxCNC-based)");
        c.percent_wrap = true; c.program_numbers = false; c.sequence_numbers = false;
        c.tool_change = "M6"; c.length_offset = true; c.canned_drill = true;
        v.push_back(c);
    }
    return v;
}

const std::vector<PostConfig>& dialects() {
    static std::vector<PostConfig> v = build_dialects();
    return v;
}
}  // namespace

const PostConfig& post_dialect(const std::string& id) {
    for (auto& c : dialects()) if (c.id == id) return c;
    // default to grbl rather than throw, so a bad id degrades gracefully
    for (auto& c : dialects()) if (c.id == "grbl") return c;
    return dialects().front();
}
std::vector<std::string> post_dialect_ids() {
    std::vector<std::string> ids;
    for (auto& c : dialects()) ids.push_back(c.id);
    return ids;
}
json post_dialects_json() {
    json a = json::array();
    for (auto& c : dialects())
        a.push_back(json{{"id", c.id}, {"name", c.name}, {"description", c.description},
                         {"lathe", c.lathe}, {"laser", c.laser_mode}, {"canned_drill", c.canned_drill}});
    return a;
}

json PostResult::to_json() const {
    return json{{"ok", ok}, {"error", error}, {"gcode", gcode}, {"line_count", line_count},
                {"tool_changes", tool_changes}, {"est_time_s", est_time_s}, {"warnings", warnings}};
}
json SheetResult::to_json() const { return json{{"html", html}, {"text", text}}; }

// ───────────────────────────────────────────────────── the NC generator ──
namespace {
struct NcGen {
    PostConfig c;
    PostOptions opt;
    std::ostringstream out;
    int seq;
    int lines = 0;
    int tool_changes = 0;
    // modal state
    std::string last_motion;
    double last_feed = -1;
    int last_rpm = -1;
    Vec3 pos{1e9, 1e9, 1e9};
    bool beam_on = false;
    bool lathe = false;
    bool diameter = false;

    NcGen(const PostConfig& cfg, const PostOptions& o) : c(cfg), opt(o), seq(cfg.seq_start) {
        lathe = c.lathe;
        diameter = c.diameter_mode;
    }

    void raw(const std::string& s) { out << s << "\n"; ++lines; }
    void blk(const std::string& s) {
        if (s.empty()) return;
        if (c.sequence_numbers || opt.sequence_numbers) {
            out << "N" << seq << " " << s << "\n";
            seq += c.seq_inc;
        } else {
            out << s << "\n";
        }
        ++lines;
    }
    void comment(const std::string& s) {
        if (!c.comments_allowed || !opt.comments) return;
        blk(c.comment_open + s + c.comment_close);
    }
    std::string axis(const char* a, double v, const NumFmt& f) { return std::string(a) + fmt_num(v, f); }

    // emit one linear/rapid move word-set with modal suppression
    void motion(const std::string& g, const Vec3& to, double feed, bool with_feed) {
        std::ostringstream b;
        bool first = true;
        if (!(c.modal_motion && g == last_motion)) { b << g; first = false; last_motion = g; }
        auto word = [&](const std::string& w) { if (!first) b << " "; b << w; first = false; };
        if (lathe) {
            double xval = diameter ? to.x * 2.0 : to.x;
            if (std::fabs(xval - (diameter ? pos.x * 2.0 : pos.x)) > 5e-7 || pos.x > 1e8) word(axis("X", xval, c.xyz));
            if (std::fabs(to.z - pos.z) > 5e-7 || pos.z > 1e8) word(axis("Z", to.z, c.xyz));
        } else {
            if (std::fabs(to.x - pos.x) > 5e-7 || pos.x > 1e8) word(axis("X", to.x, c.xyz));
            if (std::fabs(to.y - pos.y) > 5e-7 || pos.y > 1e8) word(axis("Y", to.y, c.xyz));
            if (std::fabs(to.z - pos.z) > 5e-7 || pos.z > 1e8) word(axis("Z", to.z, c.xyz));
        }
        if (with_feed && feed > 0 && (!c.modal_feed || std::fabs(feed - last_feed) > 1e-6)) {
            word(axis("F", feed, c.feed));
            last_feed = feed;
        }
        std::string line = b.str();
        if (!line.empty() && !(line == g)) blk(line);
        pos = to;
    }

    void arc(const Move& m) {
        std::string g = m.ccw ? c.ccw : c.cw;
        std::ostringstream b;
        b << g;
        last_motion = g;
        if (lathe) {
            double xval = diameter ? m.to.x * 2.0 : m.to.x;
            b << " " << axis("X", xval, c.xyz) << " " << axis("Z", m.to.z, c.xyz);
        } else {
            b << " " << axis("X", m.to.x, c.xyz) << " " << axis("Y", m.to.y, c.xyz);
            if (std::fabs(m.to.z - pos.z) > 5e-7) b << " " << axis("Z", m.to.z, c.xyz);
        }
        if (c.r_for_arcs && !c.arcs_ijk) {
            double r = std::hypot(pos.x - m.center.x, pos.y - m.center.y);
            b << " " << axis("R", r, c.arc);
        } else {
            b << " " << axis("I", m.center.x - pos.x, c.arc) << " " << axis("J", m.center.y - pos.y, c.arc);
        }
        if (m.feed > 0 && (!c.modal_feed || std::fabs(m.feed - last_feed) > 1e-6)) {
            b << " " << axis("F", m.feed, c.feed);
            last_feed = m.feed;
        }
        blk(b.str());
        pos = m.to;
    }

    void spindle(int rpm, int dir) {
        if (rpm <= 0) return;
        std::ostringstream b;
        if (c.spindle_speed_word) b << "S" << fmt_num(rpm, c.rpm) << " ";
        b << (dir < 0 ? c.spindle_ccw : c.spindle_cw);
        blk(b.str());
        if (c.wait_for_spindle && c.spindle_dwell_s > 0) blk(c.dwell + " " + c.dwell_word + fmt_num(c.spindle_dwell_s, c.feed));
        last_rpm = rpm;
    }
    void coolant(const std::string& mode) {
        if (mode == "off" || mode.empty()) return;
        if (mode == "mist") blk(c.coolant_mist);
        else if (mode == "flood" || mode == "through" || mode == "air") blk(c.coolant_flood);
    }
};

// pull the resolved rpm/feed/plunge out of a generated toolpath
void path_speeds(const Toolpath& tp, int& rpm, double& cut_feed, double& plunge_feed) {
    rpm = 0; cut_feed = 0; plunge_feed = 0;
    for (auto& m : tp.moves) {
        if (rpm == 0 && m.spindle_rpm > 0) rpm = m.spindle_rpm;
        if (cut_feed == 0 && (m.kind == MoveKind::Cut || m.kind == MoveKind::Arc) && m.feed > 0) cut_feed = m.feed;
        if (plunge_feed == 0 && m.kind == MoveKind::Plunge && m.feed > 0) plunge_feed = m.feed;
    }
    if (cut_feed == 0) cut_feed = plunge_feed > 0 ? plunge_feed : 300;
    if (plunge_feed == 0) plunge_feed = cut_feed * 0.4;
}

std::string drill_cycle_code(const PostConfig& c, const std::string& cyc) {
    if (cyc == "peck" || cyc == "deep") return c.cycle_peck;
    if (cyc == "chipbreak") return c.cycle_chipbreak;
    if (cyc == "tap" || cyc == "rigidtap") return c.cycle_tap;
    if (cyc == "ream") return c.cycle_ream;
    if (cyc == "bore" || cyc == "backbore") return c.cycle_bore;
    if (cyc == "spot") return "G82";
    return c.cycle_drill;
}

// Express a world-frame point in a work-coordinate-system's local (program) frame:
// p_local = Rᵀ·(p − origin), where R = [x_dir | y_dir | z_dir] is the WCS orientation
// and y_dir = z_dir × x_dir. For an identity WCS (origin 0, axes aligned) this is the
// identity, so unshifted/unrotated offsets pass through unchanged. Used to place canned
// drill points correctly under a per-op work offset (D2).
Vec3 to_wcs_local(const WCS& w, const Vec3& p) {
    const Vec3& x = w.x_dir;
    const Vec3& z = w.z_dir;
    Vec3 y{z.y * x.z - z.z * x.y, z.z * x.x - z.x * x.z, z.x * x.y - z.y * x.x};
    Vec3 d{p.x - w.origin.x, p.y - w.origin.y, p.z - w.origin.z};
    return Vec3{x.x * d.x + x.y * d.y + x.z * d.z,
                y.x * d.x + y.y * d.y + y.z * d.z,
                z.x * d.x + z.y * d.y + z.z * d.z};
}
}  // namespace

// ──────────────────────────────────────────────────────────── post_setup ──
PostResult post_setup(const Setup& setup, const ToolLibrary& tools, const std::string& dialect_id,
                      const std::vector<Toolpath>& toolpaths, const PostOptions& opt) {
    PostResult res;
    PostConfig c = post_dialect(dialect_id);
    if (opt.overrides.is_object() && !opt.overrides.empty()) {
        // merge field overrides
        PostConfig ov = PostConfig::from_json(opt.overrides);
        // only string/bool overrides supported via from_json; apply selectively
        if (opt.overrides.contains("canned_drill")) c.canned_drill = ov.canned_drill;
        if (opt.overrides.contains("sequence_numbers")) c.sequence_numbers = ov.sequence_numbers;
        if (opt.overrides.contains("r_for_arcs")) { c.r_for_arcs = ov.r_for_arcs; c.arcs_ijk = !ov.r_for_arcs; }
        if (opt.overrides.contains("lathe")) c.lathe = ov.lathe;
        if (opt.overrides.contains("diameter_mode")) c.diameter_mode = ov.diameter_mode;
        if (opt.overrides.contains("css_clamp")) c.css_clamp = ov.css_clamp;
    }
    NcGen g(c, opt);
    bool metric = opt.metric;

    // count distinct tools used
    int multi = 0;
    {
        int prev = -999;
        for (auto& tp : toolpaths) { if (tp.tool_number != prev) ++multi; prev = tp.tool_number; }
    }

    // ── header ──
    if (c.percent_wrap) g.raw("%");
    if (c.program_numbers) g.blk("O" + std::to_string(opt.program_number % 10000 == 0 ? 1000 : opt.program_number));
    g.comment(" " + opt.program_name + " - " + setup.name);
    g.comment(" post: " + c.name + "  machine: " + setup.machine.name);
    if (!c.program_begin.empty()) g.blk(c.program_begin);
    g.blk(metric ? c.units_metric : c.units_imperial);
    g.blk(c.abs_mode);
    if (!g.lathe) g.blk(c.plane_xy);
    if (c.comp_supported) g.blk(c.comp_off);
    if (c.canned_drill) g.blk(c.cycle_cancel);

    // ── per operation ──
    int last_tool = -999;
    double est = 0;
    // index operations by id for canned-cycle metadata
    for (const Toolpath& tp : toolpaths) {
        est += tp.cycle_time_s;
        const Operation* op = nullptr;
        for (auto& o : setup.operations) if (o.id == tp.op_id) { op = &o; break; }
        int rpm; double cutF, plungeF;
        path_speeds(tp, rpm, cutF, plungeF);

        // tool change
        if (tp.tool_number != last_tool) {
            const Tool* t = tools.find(tp.tool_number);
            g.comment(t ? (" T" + std::to_string(tp.tool_number) + " " + t->name) : (" T" + std::to_string(tp.tool_number)));
            std::ostringstream tc;
            tc << "T" << tp.tool_number;
            if (!c.tool_change.empty()) {
                if (c.tool_word_before_change) { g.blk(tc.str()); g.blk(c.tool_change); }
                else g.blk(c.tool_change + " " + tc.str());
                ++g.tool_changes;
            }
            if (c.length_offset) g.blk(c.length_offset_code + " H" + std::to_string(tp.tool_number));
            last_tool = tp.tool_number;
            g.last_rpm = -1;  // force respeed
        }
        // work offset + spindle + coolant at op start
        if (op) {
            if (c.work_offset) {
                const WCS& w = op->wcs ? *op->wcs : setup.wcs;
                if (w.offset_sub > 0)  // extended work offset: G54.1 P<n> (D4)
                    g.blk("G" + std::to_string(w.offset) + ".1 P" + std::to_string(w.offset_sub));
                else
                    g.blk("G" + std::to_string(w.offset));
            }
        }
        bool head = (setup.machine.kind == MachineKind::Laser || setup.machine.kind == MachineKind::Plasma ||
                     setup.machine.kind == MachineKind::Waterjet);
        if (g.lathe && op && op->surface_speed_mode == "css") {
            // clamp max spindle rpm BEFORE engaging constant-surface-speed, or the
            // spindle overspeeds as the turned diameter approaches zero (D9). Fanuc/Haas
            // require G50 S<max> immediately ahead of G96.
            if (!c.css_clamp.empty() && op->max_turn_rpm > 0)
                g.blk(c.css_clamp + " S" + fmt_num(op->max_turn_rpm, g.c.rpm));
            g.blk(c.css_on + " S" + fmt_num(op->css > 0 ? op->css : rpm, g.c.rpm));
        }
        if (!head && rpm > 0 && rpm != g.last_rpm) g.spindle(rpm, op ? op->spindle_dir : 1);
        if (!head && op) g.coolant(op->coolant);

        // ── drilling canned cycle path ──
        bool drilled_canned = false;
        if (op && op->type == OpType::Drill && c.canned_drill && op->geometry.contains("points")) {
            std::string code = drill_cycle_code(c, op->cycle);
            double Rp = op->heights.retract;
            double Zb = op->heights.bottom - op->tip_through;
            double F = plungeF;
            g.blk(c.cycle_retract_g99);
            bool first = true;
            for (auto& pj : op->geometry["points"]) {
                Vec3 p = vec3_from_json(pj);
                // a per-op work offset shifts/rotates the program origin; express the
                // hole in that frame so the canned-cycle XY land on the real hole (D2).
                if (op->wcs) p = to_wcs_local(*op->wcs, p);
                std::ostringstream b;
                if (first) {
                    b << code << " X" << fmt_num(p.x, c.xyz) << " Y" << fmt_num(p.y, c.xyz)
                      << " Z" << fmt_num(Zb, c.xyz) << " R" << fmt_num(Rp, c.xyz);
                    if (op->cycle == "peck" || op->cycle == "deep" || op->cycle == "chipbreak")
                        b << " Q" << fmt_num(op->peck > 0 ? op->peck : 1.0, c.xyz);
                    if (op->cycle == "spot" && op->dwell > 0)
                        b << " P" << fmt_num(c.dwell_seconds ? op->dwell : op->dwell * 1000, c.feed);
                    b << " F" << fmt_num(F, c.feed);
                    first = false;
                } else {
                    b << "X" << fmt_num(p.x, c.xyz) << " Y" << fmt_num(p.y, c.xyz);
                }
                g.blk(b.str());
                g.pos = {p.x, p.y, Rp};
            }
            g.blk(c.cycle_cancel);
            drilled_canned = true;
        }

        // ── general move emission ──
        if (!drilled_canned) {
            for (const Move& m : tp.moves) {
                switch (m.kind) {
                    case MoveKind::Rapid:
                    case MoveKind::Retract:
                        if (head && g.beam_on) { g.blk(c.torch_off); g.beam_on = false; }
                        g.motion(c.rapid, m.to, 0, false);
                        break;
                    case MoveKind::Plunge:
                        if (head && !g.beam_on) {
                            std::ostringstream b; b << c.torch_on;
                            if (op && op->power > 0) b << " S" << fmt_num(op->power * c.power_max, g.c.rpm);
                            g.blk(b.str()); g.beam_on = true;
                            if (op && op->pierce_delay > 0) g.blk(c.dwell + " " + c.dwell_word + fmt_num(op->pierce_delay, c.feed));
                        }
                        g.motion(c.linear, m.to, m.feed, true);
                        break;
                    case MoveKind::Lead:
                    case MoveKind::Cut:
                        if (head && !g.beam_on) { g.blk(c.torch_on); g.beam_on = true; }
                        g.motion(c.linear, m.to, m.feed, true);
                        break;
                    case MoveKind::Arc:
                        if (c.arcs_supported) g.arc(m);
                        else g.motion(c.linear, m.to, m.feed, true);
                        break;
                    case MoveKind::Dwell:
                        g.blk(c.dwell + " " + c.dwell_word + fmt_num(c.dwell_seconds ? m.dwell_s : m.dwell_s * 1000, c.feed));
                        break;
                    case MoveKind::ToolChange:
                        break;
                }
            }
            if (head && g.beam_on) { g.blk(c.torch_off); g.beam_on = false; }
        }
    }

    // ── footer ──
    if (!g.lathe && !( setup.machine.kind == MachineKind::Laser )) g.blk(g.c.spindle_off);
    g.blk(c.coolant_off);
    if (!c.program_end.empty()) g.blk(c.program_end);
    g.blk(c.program_stop);
    if (c.percent_wrap) g.raw("%");

    // Tool re-change warning: a tool number that appears in ≥2 non-consecutive
    // op groups within this setup costs extra tool changes.
    {
        std::unordered_map<int, int> groups;
        int prev = -999;
        for (const Toolpath& tp : toolpaths) {
            if (tp.tool_number != prev) {
                ++groups[tp.tool_number];
                prev = tp.tool_number;
            }
        }
        for (const auto& kv : groups) {
            if (kv.second >= 2) {
                res.warnings.push_back(
                    "tool T" + std::to_string(kv.first) + " used in " +
                    std::to_string(kv.second) + " separated op groups — reorder to save " +
                    std::to_string(kv.second - 1) + " tool change(s)");
            }
        }
    }

    res.gcode = g.out.str();
    res.line_count = g.lines;
    res.tool_changes = g.tool_changes;
    res.est_time_s = est;
    return res;
}

PostResult post_job(const Job& job, const std::vector<std::vector<Toolpath>>& per_setup,
                    const PostOptions& opt) {
    PostResult res;
    std::ostringstream all;
    double est = 0;
    int lines = 0, tc = 0;
    for (size_t i = 0; i < job.setups.size() && i < per_setup.size(); ++i) {
        PostResult r = post_setup(job.setups[i], job.tools, job.post, per_setup[i], opt);
        if (!r.ok) return r;
        all << r.gcode;
        if (i + 1 < job.setups.size()) all << "\n";
        est += r.est_time_s; lines += r.line_count; tc += r.tool_changes;
        for (auto& w : r.warnings) res.warnings.push_back(w);
    }
    res.gcode = all.str();
    res.est_time_s = est; res.line_count = lines; res.tool_changes = tc;
    return res;
}

// ──────────────────────────────────────────────────────── setup sheet ──
SheetResult setup_sheet(const Job& job, const std::vector<std::vector<Toolpath>>& per_setup,
                        const std::string& dialect_id) {
    SheetResult s;
    std::ostringstream h, t;
    h << "<html><head><meta charset='utf-8'><style>"
         "body{font-family:sans-serif;margin:20px;color:#222}"
         "h1{font-size:20px}h2{font-size:16px;margin-top:18px}"
         "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #bbb;padding:4px 8px;font-size:13px}"
         "th{background:#eee}</style></head><body>";
    h << "<h1>Setup Sheet — " << job.name << "</h1>";
    h << "<p>Post: " << post_dialect(dialect_id).name << " &nbsp; Units: " << job.units << "</p>";
    t << "SETUP SHEET — " << job.name << "\n";
    t << "Post: " << post_dialect(dialect_id).name << "   Units: " << job.units << "\n";

    // tool table
    h << "<h2>Tools</h2><table><tr><th>T#</th><th>Type</th><th>Dia</th><th>Flutes</th><th>Name</th></tr>";
    t << "\nTOOLS\n  T#  Type            Dia    Fl  Name\n";
    for (auto& tl : job.tools.tools) {
        h << "<tr><td>" << tl.number << "</td><td>" << to_string(tl.type) << "</td><td>"
          << tl.diameter << "</td><td>" << tl.flutes << "</td><td>" << tl.name << "</td></tr>";
        char b[160];
        std::snprintf(b, sizeof(b), "  %-3d %-15s %-6.2f %-3d %s\n", tl.number, to_string(tl.type),
                      tl.diameter, tl.flutes, tl.name.c_str());
        t << b;
    }
    h << "</table>";

    double total = 0;
    for (size_t i = 0; i < job.setups.size(); ++i) {
        const Setup& su = job.setups[i];
        h << "<h2>" << su.name << " — " << su.machine.name << "</h2>";
        h << "<table><tr><th>#</th><th>Operation</th><th>Type</th><th>T#</th><th>Cut len (mm)</th><th>Time</th></tr>";
        t << "\n" << su.name << " — " << su.machine.name << "\n";
        t << "  #  Operation          Type          T#  CutLen   Time\n";
        const std::vector<Toolpath>* tps = (i < per_setup.size()) ? &per_setup[i] : nullptr;
        int idx = 1;
        for (const Operation& op : su.operations) {
            double clen = 0, tsec = 0;
            if (tps) for (auto& tp : *tps) if (tp.op_id == op.id) { clen = tp.cut_length; tsec = tp.cycle_time_s; }
            total += tsec;
            int mm = (int)(tsec / 60), ss = (int)tsec % 60;
            h << "<tr><td>" << idx << "</td><td>" << op.name << "</td><td>" << to_string(op.type)
              << "</td><td>" << op.tool_number << "</td><td>" << (long)clen << "</td><td>"
              << mm << "m" << ss << "s</td></tr>";
            char b[200];
            std::snprintf(b, sizeof(b), "  %-2d %-18s %-13s %-3d %-8ld %dm%02ds\n", idx, op.name.c_str(),
                          to_string(op.type), op.tool_number, (long)clen, mm, ss);
            t << b;
            ++idx;
        }
        h << "</table>";
    }
    int tm = (int)(total / 60), ts2 = (int)total % 60;
    h << "<p><b>Estimated total machining time: " << tm << "m " << ts2 << "s</b></p></body></html>";
    t << "\nEstimated total machining time: " << tm << "m " << ts2 << "s\n";
    s.html = h.str();
    s.text = t.str();
    return s;
}

}  // namespace cam
}  // namespace chudware
