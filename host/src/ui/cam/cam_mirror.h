// CamMirror — parse CAM tree/toolpath JSON into typed rows and cached native-renderer geometry.
// Coordinates are WCS millimetres (X right, Y back, Z up — the tool axis). This mirror never
// touches ImGui or the renderer; it depends only on nlohmann::json.
#pragma once

#include <array>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct Mesh;  // fwd (host/src/mesh.h)

namespace ui {

struct CamOpRow {
    std::string id;
    std::string name;
    std::string type;
    int tool_number = 0;
    bool suppressed = false;
    double stepover = 0.0;
    double stepdown = 0.0;
    double stock_leave = 0.0;  // serialized as stock_to_leave
    double feed = 0.0;
    double rpm = 0.0;
    double top = 0.0;
    double bottom = 0.0;
    double clearance = 0.0;
    double retract = 0.0;
};

struct CamSetupRow {
    std::string id;
    std::string name;
    std::string machine;
    nlohmann::json stock;
    std::vector<float> stockSegments;  // cached 12 box edges, empty for non-box stock
    std::vector<CamOpRow> ops;
};

struct CamToolRow {
    int number = 0;
    std::string name;
    std::string type;
    double diameter = 0.0;
};

// Classified toolpath geometry. Segment arrays are flat GL_LINES batches. *T0/*T are the true
// global start/end seconds for each corresponding segment, even when move classes interleave.
// *Dir arrays cache bounded direction triangles (9 xyz floats per triangle) for the native overlay.
struct CamBackplot {
    std::vector<float> rapid, cut, lead, plunge, arc;
    std::vector<float> rapidT0, cutT0, leadT0, plungeT0, arcT0;
    std::vector<float> rapidT, cutT, leadT, plungeT, arcT;
    std::vector<float> rapidDir, cutDir, leadDir, plungeDir, arcDir;
    double totalTime = 0;
    float minx = 0, miny = 0, minz = 0, maxx = 0, maxy = 0, maxz = 0;
    bool empty() const { return cut.empty() && rapid.empty() && lead.empty() && plunge.empty() && arc.empty(); }
    void clear() {
        rapid.clear(); cut.clear(); lead.clear(); plunge.clear(); arc.clear();
        rapidT0.clear(); cutT0.clear(); leadT0.clear(); plungeT0.clear(); arcT0.clear();
        rapidT.clear(); cutT.clear(); leadT.clear(); plungeT.clear(); arcT.clear();
        rapidDir.clear(); cutDir.clear(); leadDir.clear(); plungeDir.clear(); arcDir.clear();
        totalTime = 0;
        minx = miny = minz = maxx = maxy = maxz = 0;
    }
};

class CamMirror {
public:
    // Rebuild the tree vectors from a cam_tree / cam_apply_ops response ({revision, tree}).
    void loadTree(const nlohmann::json& resp);
    // Rebuild the backplot from a cam_generate response ({toolpaths:[...]}).
    void loadToolpaths(const nlohmann::json& resp);

    int revision() const { return revision_; }
    const std::string& name() const { return name_; }
    const std::string& post() const { return post_; }
    const std::vector<CamSetupRow>& setups() const { return setups_; }
    const std::vector<CamToolRow>& tools() const { return tools_; }
    const CamBackplot& backplot() const { return bp_; }
    void clearToolpaths() { bp_.clear(); }
    // Rapid rates from the tree's machine_detail (fallbacks 5000/3000 mm/min).
    double rapidXY = 5000, rapidZ = 3000;
    void clear() {
        setups_.clear(); tools_.clear(); bp_.clear(); name_.clear(); post_.clear(); revision_ = -1;
        rapidXY = 5000; rapidZ = 3000;
    }

private:
    int revision_ = -1;
    std::string name_;
    std::string post_;
    std::vector<CamSetupRow> setups_;
    std::vector<CamToolRow> tools_;
    CamBackplot bp_;
};

}  // namespace ui

namespace ui {
// Build a renderable mesh from an aicad.cam_sim_frames/1 field dict
// ({nx,ny,res,lo:[x,y],z:[...]}). Vertex per grid node, two triangles per cell,
// per-vertex normals from central differences. Grids >250k nodes build at step 2.
// Returns false on malformed input (caller keeps the previous mesh).
bool camFieldToMesh(const nlohmann::json& field, Mesh& out);
}  // namespace ui
