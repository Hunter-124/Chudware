// ECAD -> 3D bridge implementation — C++ port of engine/ecad/threed.py.
// See ecad_3d.h for scope/parity notes.

#include "geom/ecad_3d.h"

#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

#include <BRep_Builder.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakePolygon.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <GProp_GProps.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_Writer.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include "ecad/ops.h"      // OpError
#include "ecad/schema.h"   // pad_world
#include "geom/booleans.h"
#include "geom/features.h"  // make_box_at

using nlohmann::json;

namespace aicad::ecad {

namespace {

// Endpoint-chaining tolerance for stitching outline segments into a closed wire (mm).
constexpr double kOutlineTol = 1e-3;
// Tessellation count for arc/circle outline edges when we fall back to a polyline.
constexpr int kArcSegs = 64;

// Default per-attr proxy heights (mm) when a footprint has no explicit height.
double proxy_height_for_attr(const std::string& attr) {
    if (attr == "smd") return 1.0;
    if (attr == "tht") return 5.0;
    if (attr == "np_tht") return 5.0;
    if (attr == "virtual") return 1.0;
    return 1.5;  // _PROXY_HEIGHT_DEFAULT
}
constexpr double kProxyMinSpan = 0.5;
constexpr double kProxyPadMargin = 0.5;

constexpr double kPi = 3.14159265358979323846;

struct Pt {
    double x, y;
};

double jnum(const json& j, double dflt = 0.0) {
    return j.is_number() ? j.get<double>() : dflt;
}

// Polyline points along an arc from a0 to a1 (deg, CCW), inclusive of both ends.
// Mirrors threed._arc_points EXACTLY (same n formula, same sampling).
std::vector<Pt> arc_points(double cx, double cy, double radius, double a0_deg, double a1_deg) {
    double a0 = a0_deg * kPi / 180.0;
    double a1 = a1_deg * kPi / 180.0;
    if (a1 <= a0) a1 += 2 * kPi;
    int n = std::max(2, static_cast<int>(kArcSegs * (a1 - a0) / (2 * kPi)) + 1);
    std::vector<Pt> out;
    out.reserve(n + 1);
    for (int i = 0; i <= n; ++i) {
        double t = a0 + (a1 - a0) * static_cast<double>(i) / static_cast<double>(n);
        out.push_back({cx + radius * std::cos(t), cy + radius * std::sin(t)});
    }
    return out;
}

struct Circle {
    double cx, cy, r;
};
struct Chord {
    Pt a, b;
};

// Split outline shapes into full circles + straight chords (lines + tessellated arcs).
// Mirrors threed._outline_segments.
void outline_segments(const json& outline, std::vector<Circle>& circles,
                      std::vector<Chord>& chords) {
    json shapes = outline.contains("shapes") && outline["shapes"].is_array()
                      ? outline["shapes"]
                      : json::array();
    for (const auto& sh : shapes) {
        std::string stype = sh.value("type", std::string("line"));
        if (stype == "line") {
            const auto& p1 = sh.at("p1");
            const auto& p2 = sh.at("p2");
            chords.push_back({{jnum(p1[0]), jnum(p1[1])}, {jnum(p2[0]), jnum(p2[1])}});
        } else if (stype == "arc") {
            const auto& c = sh.at("center");
            auto pts = arc_points(jnum(c[0]), jnum(c[1]), jnum(sh.at("radius")),
                                  sh.contains("a0") ? jnum(sh["a0"]) : 0.0,
                                  sh.contains("a1") ? jnum(sh["a1"]) : 360.0);
            for (size_t i = 0; i + 1 < pts.size(); ++i)
                chords.push_back({pts[i], pts[i + 1]});
        } else if (stype == "circle") {
            const auto& c = sh.at("center");
            circles.push_back({jnum(c[0]), jnum(c[1]), jnum(sh.at("radius"))});
        } else {
            throw OpError("invalid_op",
                          "unsupported outline shape type '" + stype + "'",
                          json{{"reason", "bad outline shape"}});
        }
    }
}

bool close_pt(const Pt& a, const Pt& b) {
    return std::abs(a.x - b.x) <= kOutlineTol && std::abs(a.y - b.y) <= kOutlineTol;
}

// Stitch chords into ordered closed point loops by endpoint matching.
// Mirrors threed._chain_loops (raises on an open chain).
std::vector<std::vector<Pt>> chain_loops(std::vector<Chord> chords) {
    std::vector<std::vector<Pt>> loops;
    while (!chords.empty()) {
        Chord first = chords.front();
        chords.erase(chords.begin());
        std::vector<Pt> loop{first.a, first.b};
        bool progressed = true;
        while (!close_pt(loop.front(), loop.back()) && progressed) {
            progressed = false;
            for (size_t i = 0; i < chords.size(); ++i) {
                if (close_pt(chords[i].a, loop.back())) {
                    loop.push_back(chords[i].b);
                    chords.erase(chords.begin() + i);
                    progressed = true;
                    break;
                }
                if (close_pt(chords[i].b, loop.back())) {
                    loop.push_back(chords[i].a);
                    chords.erase(chords.begin() + i);
                    progressed = true;
                    break;
                }
            }
        }
        if (!close_pt(loop.front(), loop.back())) {
            throw OpError("invalid_op",
                          "Edge.Cuts outline does not form a closed loop. The board "
                          "outline must be one or more closed contours.",
                          json{{"reason", "open outline"}});
        }
        loop.pop_back();  // drop duplicated closing point
        loops.push_back(std::move(loop));
    }
    return loops;
}

// Signed shoelace area of a closed point loop.
double loop_area(const std::vector<Pt>& loop) {
    double a = 0.0;
    size_t n = loop.size();
    for (size_t i = 0; i < n; ++i) {
        const Pt& p = loop[i];
        const Pt& q = loop[(i + 1) % n];
        a += p.x * q.y - q.x * p.y;
    }
    return a / 2.0;
}

// Build a planar face (z=0) from a CCW/CW point loop via MakePolygon -> MakeFace.
TopoDS_Face polygon_face(const std::vector<Pt>& loop) {
    BRepBuilderAPI_MakePolygon mp;
    for (const auto& p : loop) mp.Add(gp_Pnt(p.x, p.y, 0.0));
    mp.Close();
    if (!mp.IsDone()) return TopoDS_Face();
    BRepBuilderAPI_MakeFace mf(mp.Wire(), /*OnlyPlane=*/true);
    if (!mf.IsDone()) return TopoDS_Face();
    return mf.Face();
}

TopoDS_Face circle_face(double cx, double cy, double r) {
    BRepBuilderAPI_MakeEdge me(gp_Circ(gp_Ax2(gp_Pnt(cx, cy, 0.0), gp_Dir(0, 0, 1)), r));
    if (!me.IsDone()) return TopoDS_Face();
    TopoDS_Wire w = BRepBuilderAPI_MakeWire(me.Edge()).Wire();
    BRepBuilderAPI_MakeFace mf(w, true);
    if (!mf.IsDone()) return TopoDS_Face();
    return mf.Face();
}

struct Cand {
    double area;
    TopoDS_Face face;
};

// Build the Edge.Cuts outline face: largest-area loop = outer, the rest subtracted
// as holes. Mirrors threed._outline_face (returns face + outer area).
TopoDS_Face outline_face(const json& outline, double& outer_area_out) {
    if (!outline.is_object() || !outline.contains("shapes") ||
        !outline["shapes"].is_array() || outline["shapes"].empty()) {
        throw OpError("invalid_op",
                      "board has no Edge.Cuts outline shapes; cannot build a substrate.",
                      json{{"reason", "empty outline"}});
    }
    std::vector<Circle> circles;
    std::vector<Chord> chords;
    outline_segments(outline, circles, chords);
    std::vector<std::vector<Pt>> poly_loops = chain_loops(std::move(chords));

    std::vector<Cand> cand;
    for (const auto& loop : poly_loops) {
        double area = std::abs(loop_area(loop));
        if (area <= 1e-9) continue;
        TopoDS_Face f = polygon_face(loop);
        if (f.IsNull()) continue;
        cand.push_back({area, f});
    }
    for (const auto& c : circles) {
        double area = kPi * c.r * c.r;
        TopoDS_Face f = circle_face(c.cx, c.cy, c.r);
        if (f.IsNull()) continue;
        cand.push_back({area, f});
    }
    if (cand.empty()) {
        throw OpError("invalid_op", "Edge.Cuts outline produced no usable closed region.",
                      json{{"reason", "no region"}});
    }
    std::stable_sort(cand.begin(), cand.end(),
                     [](const Cand& a, const Cand& b) { return a.area > b.area; });
    outer_area_out = cand.front().area;
    TopoDS_Face face = cand.front().face;
    // Subtract inner loops as holes by adding their (reversed) outer wires to the
    // outer face — mirrors the oracle's `face = face - inner` chain (a non-overlapping
    // extra loop simply fails MakeFace and is ignored, matching the BLE001 guard).
    if (cand.size() > 1) {
        BRepBuilderAPI_MakeFace mf(face);
        bool any = false;
        for (size_t i = 1; i < cand.size(); ++i) {
            for (TopExp_Explorer ex(cand[i].face, TopAbs_WIRE); ex.More(); ex.Next()) {
                TopoDS_Wire iw = TopoDS::Wire(ex.Current());
                mf.Add(TopoDS::Wire(iw.Reversed()));
                any = true;
                break;  // outer wire only
            }
        }
        if (any && mf.IsDone()) face = mf.Face();
    }
    return face;
}

// World position of a footprint pad. Bottom-side footprints mirror across x.
// Reuses the ECAD schema helper for exact parity with the oracle.
Pt pad_world_pt(const json& fp, const json& pad) {
    auto [wx, wy] = aicad::ecad::schema::pad_world(fp, pad);
    return {wx, wy};
}

struct Drill {
    double x, y, dia;
};

// Yield (x, y, dia) for every plated/NPTH drill: tht/np_tht pads + vias.
// Mirrors threed._iter_drills.
std::vector<Drill> iter_drills(const json& board) {
    std::vector<Drill> out;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (!fp.contains("pads") || !fp["pads"].is_array()) continue;
            for (const auto& pad : fp["pads"]) {
                if (!pad.contains("drill") || pad["drill"].is_null()) continue;
                const json& drill = pad["drill"];
                double dia = drill.contains("diameter") ? jnum(drill["diameter"]) : 0.0;
                if (dia <= 0.0) continue;
                Pt w = pad_world_pt(fp, pad);
                double ox = 0.0, oy = 0.0;
                if (drill.contains("offset") && drill["offset"].is_array() &&
                    drill["offset"].size() >= 2) {
                    ox = jnum(drill["offset"][0]);
                    oy = jnum(drill["offset"][1]);
                }
                out.push_back({w.x + ox, w.y + oy, dia});
            }
        }
    }
    if (board.contains("vias") && board["vias"].is_array()) {
        for (const auto& via : board["vias"]) {
            double dia = via.contains("drill") ? jnum(via["drill"]) : 0.0;
            if (dia <= 0.0) continue;
            double ax = 0.0, ay = 0.0;
            if (via.contains("at") && via["at"].is_array() && via["at"].size() >= 2) {
                ax = jnum(via["at"][0]);
                ay = jnum(via["at"][1]);
            }
            out.push_back({ax, ay, dia});
        }
    }
    return out;
}

double shape_volume(const TopoDS_Shape& s) {
    if (s.IsNull()) return 0.0;
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}

// ---- footprint extent (proxy box width/height in local frame) — threed._footprint_extent
void footprint_extent(const json& fp, double& w, double& h) {
    std::vector<double> xs, ys;
    // Courtyard graphics (most accurate collision extent).
    std::vector<Pt> crtyd;
    if (fp.contains("graphics") && fp["graphics"].is_array()) {
        for (const auto& g : fp["graphics"]) {
            std::string layer = g.value("layer", std::string());
            if (layer != "F.CrtYd" && layer != "B.CrtYd") continue;
            std::string gtype = g.value("type", std::string());
            if (gtype == "line") {
                crtyd.push_back({jnum(g.at("p1")[0]), jnum(g.at("p1")[1])});
                crtyd.push_back({jnum(g.at("p2")[0]), jnum(g.at("p2")[1])});
            } else if (gtype == "rect") {
                const json& p1 = g.contains("p1") ? g["p1"] : (g.contains("start") ? g["start"] : json());
                const json& p2 = g.contains("p2") ? g["p2"] : (g.contains("end") ? g["end"] : json());
                if (p1.is_array() && p2.is_array()) {
                    crtyd.push_back({jnum(p1[0]), jnum(p1[1])});
                    crtyd.push_back({jnum(p2[0]), jnum(p2[1])});
                }
            } else if (gtype == "circle") {
                double cx = jnum(g.at("center")[0]), cy = jnum(g.at("center")[1]);
                double r = jnum(g.at("radius"));
                crtyd.push_back({cx - r, cy - r});
                crtyd.push_back({cx + r, cy + r});
            } else if (gtype == "poly" && g.contains("points") && g["points"].is_array()) {
                for (const auto& p : g["points"]) crtyd.push_back({jnum(p[0]), jnum(p[1])});
            }
        }
    }
    if (!crtyd.empty()) {
        for (const auto& p : crtyd) {
            xs.push_back(p.x);
            ys.push_back(p.y);
        }
    } else if (fp.contains("pads") && fp["pads"].is_array()) {
        // Pad extent fallback.
        for (const auto& pad : fp["pads"]) {
            double px = jnum(pad.at("pos")[0]), py = jnum(pad.at("pos")[1]);
            double sw = 0.5, sh = 0.5;
            if (pad.contains("size") && pad["size"].is_array() && pad["size"].size() >= 2) {
                sw = jnum(pad["size"][0]);
                sh = jnum(pad["size"][1]);
            }
            xs.push_back(px - sw / 2.0);
            xs.push_back(px + sw / 2.0);
            ys.push_back(py - sh / 2.0);
            ys.push_back(py + sh / 2.0);
        }
        if (!xs.empty()) {
            double minx = *std::min_element(xs.begin(), xs.end());
            double maxx = *std::max_element(xs.begin(), xs.end());
            double miny = *std::min_element(ys.begin(), ys.end());
            double maxy = *std::max_element(ys.begin(), ys.end());
            xs = {minx - kProxyPadMargin, maxx + kProxyPadMargin};
            ys = {miny - kProxyPadMargin, maxy + kProxyPadMargin};
        }
    }
    if (xs.empty() || ys.empty()) {
        w = kProxyMinSpan;
        h = kProxyMinSpan;
        return;
    }
    double minx = *std::min_element(xs.begin(), xs.end());
    double maxx = *std::max_element(xs.begin(), xs.end());
    double miny = *std::min_element(ys.begin(), ys.end());
    double maxy = *std::max_element(ys.begin(), ys.end());
    w = std::max(maxx - minx, kProxyMinSpan);
    h = std::max(maxy - miny, kProxyMinSpan);
}

double proxy_height(const json& fp) {
    if (fp.contains("fields") && fp["fields"].is_object() &&
        fp["fields"].contains("height")) {
        const json& hj = fp["fields"]["height"];
        if (hj.is_number()) {
            double hv = hj.get<double>();
            if (hv > 0) return hv;
        }
    }
    return proxy_height_for_attr(fp.value("attr", std::string("smd")));
}

}  // namespace

// Build the board 3D model. Returns the summary/volume JSON; when `shapes_out` is
// non-null, every built solid (substrate first, then placed component proxies) is
// appended to it so a caller can export the assembly to STEP. The summary + volume
// numbers are computed identically whether or not shapes are collected, so
// board_to_3d (summary only) and board_to_3d_step (summary + STEP) stay in parity.
static json build_board_3d(const json& board, const json& opts_in,
                           std::vector<TopoDS_Shape>* shapes_out) {
    json opts = opts_in.is_object() ? opts_in : json::object();
    bool do_components = opts.value("components", true);
    std::string z_origin = opts.value("z_origin", std::string("bottom"));
    // (copper extrusion is bounding-realism only in the oracle and does not affect
    //  board_volume; deferred here — see header.)

    json summary = {
        {"outline_area", 0.0}, {"thickness", 0.0}, {"board_volume", 0.0},
        {"n_holes", 0},        {"n_components", 0}, {"n_proxies", 0},
        {"n_models", 0},       {"n_skipped", 0},    {"warnings", json::array()},
    };

    // ---- substrate -------------------------------------------------------------
    json stack = board.contains("stackup") && board["stackup"].is_object()
                     ? board["stackup"]
                     : json::object();
    double thickness = stack.contains("thickness") ? jnum(stack["thickness"], 1.6) : 1.6;
    if (thickness <= 0.0) {
        throw OpError("invalid_op",
                      "stackup.thickness must be > 0, got " + std::to_string(thickness),
                      json{{"reason", "bad thickness"}});
    }

    double outer_area = 0.0;
    json outline = board.contains("outline") && board["outline"].is_object()
                       ? board["outline"]
                       : json::object();
    TopoDS_Face face = outline_face(outline, outer_area);
    summary["outline_area"] = outer_area;
    summary["thickness"] = thickness;

    double zb = (z_origin == "center") ? -thickness / 2.0 : 0.0;

    // Extrude the face from z=zb up by thickness.
    if (zb != 0.0) {
        gp_Trsf tr;
        tr.SetTranslation(gp_Vec(0, 0, zb));
        face = TopoDS::Face(BRepBuilderAPI_Transform(face, tr, true).Shape());
    }
    TopoDS_Shape solid =
        BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, thickness)).Shape();

    // Subtract drill cylinders (slightly over-tall so the cut is clean through).
    std::vector<Drill> holes = iter_drills(board);
    summary["n_holes"] = static_cast<int>(holes.size());
    double overshoot = thickness * 0.1 + 0.1;
    for (const auto& d : holes) {
        // Cylinder centered at z = zb + thickness/2, growing -overshoot..+(t+overshoot).
        gp_Ax2 ax(gp_Pnt(d.x, d.y, zb - overshoot), gp_Dir(0, 0, 1));
        TopoDS_Shape cyl =
            BRepPrimAPI_MakeCylinder(ax, d.dia / 2.0, thickness + 2 * overshoot).Shape();
        TopoDS_Shape out = ::geom::cut(solid, cyl);
        if (out.IsNull()) {
            summary["warnings"].push_back("drill subtraction failed");
            continue;
        }
        solid = out;
    }

    double substrate_volume = shape_volume(solid);
    summary["board_volume"] = substrate_volume;

    if (shapes_out) shapes_out->push_back(solid);

    int body_count = 1;  // substrate
    double total_volume = substrate_volume;

    // ---- components (analytic box proxies) -------------------------------------
    if (do_components && board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            // _is_on_board: a footprint contributes unless on_board == false.
            if (fp.contains("on_board") && fp["on_board"].is_boolean() &&
                !fp["on_board"].get<bool>())
                continue;
            bool has_pads = fp.contains("pads") && fp["pads"].is_array() && !fp["pads"].empty();
            if (!has_pads) {
                std::string ref = fp.value("ref", fp.value("id", std::string("?")));
                summary["warnings"].push_back("footprint '" + ref + "' has no pads — skipped.");
                summary["n_skipped"] = summary["n_skipped"].get<int>() + 1;
                continue;
            }
            // model_3d import is DEFERRED -> always box proxy (matches the oracle's
            // box-proxy fallback when model_3d is None / unloadable).
            bool has_model =
                fp.contains("model_3d") && fp["model_3d"].is_string() && !fp["model_3d"].get<std::string>().empty();
            if (has_model) {
                // We do not load external STEP — record as a model is requested but, to
                // keep the volume gate exact, treat as a deferred proxy with a warning.
                summary["warnings"].push_back(
                    "model_3d import deferred — using box proxy for '" +
                    fp.value("ref", std::string("CMP")) + "'.");
            }

            double w, h;
            footprint_extent(fp, w, h);
            double ht = proxy_height(fp);

            // Local proxy: box centered at origin in x/y, base at z=0 (Box(w,h,ht) lifted
            // by ht/2 -> z in [0, ht]). Place per footprint pose. Mirrors _proxy_shape +
            // _place_component.
            double px = 0.0, py = 0.0;
            if (fp.contains("pos") && fp["pos"].is_array() && fp["pos"].size() >= 2) {
                px = jnum(fp["pos"][0]);
                py = jnum(fp["pos"][1]);
            }
            double angle = fp.contains("angle") ? jnum(fp["angle"]) : 0.0;
            std::string side = fp.value("side", std::string("top"));

            // Build local proxy box (min corner at -w/2,-h/2,0).
            TopoDS_Shape proxy = ::geom::make_box_at(-w / 2.0, -h / 2.0, 0.0, w, h, ht);

            gp_Trsf place;
            if (side == "bottom") {
                // Flip upside down (Rot 180 about X) then drop under the bottom face.
                gp_Trsf flip;
                flip.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), kPi);
                gp_Trsf rot;
                rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), -angle * kPi / 180.0);
                gp_Trsf trans;
                trans.SetTranslation(gp_Vec(px, py, zb));
                place = trans * rot * flip;
            } else {
                gp_Trsf rot;
                rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle * kPi / 180.0);
                gp_Trsf trans;
                trans.SetTranslation(gp_Vec(px, py, zb + thickness));
                place = trans * rot;
            }
            TopoDS_Shape placed = BRepBuilderAPI_Transform(proxy, place, true).Shape();
            if (placed.IsNull()) {
                summary["warnings"].push_back("component placement failed");
                summary["n_skipped"] = summary["n_skipped"].get<int>() + 1;
                continue;
            }
            total_volume += shape_volume(placed);
            if (shapes_out) shapes_out->push_back(placed);
            summary["n_proxies"] = summary["n_proxies"].get<int>() + 1;
            summary["n_components"] = summary["n_components"].get<int>() + 1;
            body_count++;
        }
    }

    return json{
        {"summary", summary},
        {"body_count", body_count},
        {"substrate_volume", substrate_volume},
        {"total_volume", total_volume},
    };
}

json board_to_3d(const json& board, const json& opts) {
    return build_board_3d(board, opts, nullptr);
}

json board_to_3d_step(const json& board, const json& opts, const std::string& out_path) {
    std::vector<TopoDS_Shape> shapes;
    json result = build_board_3d(board, opts, &shapes);

    // Assemble into a single compound and write STEP (mirrors threed.py's
    // Compound(children).export_step). OCCT 7.9.3 on both engines.
    TopoDS_Compound comp;
    BRep_Builder bb;
    bb.MakeCompound(comp);
    for (const TopoDS_Shape& s : shapes)
        if (!s.IsNull()) bb.Add(comp, s);

    STEPControl_Writer writer;
    if (writer.Transfer(comp, STEPControl_AsIs) != IFSelect_RetDone)
        throw OpError("io_error", "ecad_to_3d: STEP transfer failed",
                      json{{"reason", "step transfer"}, {"path", out_path}});
    if (writer.Write(out_path.c_str()) != IFSelect_RetDone)
        throw OpError("io_error", "ecad_to_3d: STEP write failed for '" + out_path + "'",
                      json{{"reason", "step write"}, {"path", out_path}});

    result["step_path"] = out_path;
    return result;
}

}  // namespace aicad::ecad
