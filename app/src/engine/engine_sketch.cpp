// Sketch-based features. Builds the full sketch + profile + extrude op batch the kernel expects
// (see cad_guide). Separate TU so it composes without touching the (collaborator-hot) engine.cpp.
#include "engine/engine.h"
using nlohmann::json;

namespace eng {

// One ops batch: a sketch on `plane`, a rectangle (centered, w x h) of 4 lines, then an extrude
// of that region. mode = "boss" | "cut". $0 refers to op index 0 (the sketch).
OpsResult Engine::sketchRectExtrude(const std::string& plane, double w, double h, double depth,
                                    const std::string& mode) {
    double x=w*0.5, y=h*0.5;
    json ops = json::array();
    ops.push_back({{"op","add_feature"},{"feature",{
        {"type","sketch"},{"name","Sketch"},
        {"params",{{"entities",json::array()},{"constraints",json::array()}}},
        {"refs",{{"plane",plane}}}}}});
    auto line=[&](double x1,double y1,double x2,double y2){
        ops.push_back({{"op","sketch_add_entity"},{"sketch","$0"},
            {"entity",{{"type","line"},{"p1",{x1,y1}},{"p2",{x2,y2}}}}}); };
    line(-x,-y, x,-y); line(x,-y, x,y); line(x,y, -x,y); line(-x,y, -x,-y);
    ops.push_back({{"op","add_feature"},{"feature",{
        {"type","extrude"},{"name","Extrude"},
        {"params",{{"mode",mode},{"end","blind"},{"depth",dim(depth)}}},
        {"refs",{{"profile",json::array({ json{{"kind","region"},{"sketch","$0"},{"centroid",{0,0}},{"hint_index",0}} })}}}}}});
    return apply(ops);
}

// Sketch a circle (radius r) on `plane` and extrude it.
OpsResult Engine::sketchCircleExtrude(const std::string& plane, double r, double depth,
                                      const std::string& mode) {
    json ops = json::array();
    ops.push_back({{"op","add_feature"},{"feature",{
        {"type","sketch"},{"name","Sketch"},
        {"params",{{"entities",json::array()},{"constraints",json::array()}}},
        {"refs",{{"plane",plane}}}}}});
    ops.push_back({{"op","sketch_add_entity"},{"sketch","$0"},
        {"entity",{{"type","circle"},{"center",{0,0}},{"radius",r}}}});
    ops.push_back({{"op","add_feature"},{"feature",{
        {"type","extrude"},{"name","Extrude"},
        {"params",{{"mode",mode},{"end","blind"},{"depth",dim(depth)}}},
        {"refs",{{"profile",json::array({ json{{"kind","region"},{"sketch","$0"},{"centroid",{0,0}},{"hint_index",0}} })}}}}}});
    return apply(ops);
}

} // namespace eng
