// CamMirror — a read-only, in-process mirror of one open CAM job, built from the JSON
// the C++ core returns for cam_tree / cam_apply_ops (shape: {revision, tree:{name,post,
// tools,setups:[{id,name,machine,stock,operations:[...]}]}}) and from cam_generate
// (toolpaths). It is the CAM analogue of ui::EcadMirror: it PARSES the job tree into flat
// typed vectors and the generated toolpaths into classified world-space line batches
// (rapid/cut/lead/plunge/arc) ready for Overlays::drawSegments and an in-panel backplot.
//
// Coordinates are WCS millimetres (X right, Y back, Z up — the tool axis). This mirror
// never touches ImGui or the renderer; it depends only on nlohmann::json.
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
};

struct CamSetupRow {
    std::string id;
    std::string name;
    std::string machine;
    nlohmann::json stock;   // raw stock dict (for the stock form / re-extract)
    std::vector<CamOpRow> ops;
};

struct CamToolRow {
    int number = 0;
    std::string name;
    std::string type;
    double diameter = 0.0;
};

// Classified toolpath geometry: flat [x0,y0,z0,x1,y1,z1,...] GL_LINES batches per move
// class, plus the XY/Z bounds for the in-panel backplot fit. The *T vectors carry the
// cumulative SECONDS at each segment's end (one value per 6-float segment) so the
// viewport playhead can split the backplot at a time without re-walking moves.
struct CamBackplot {
    std::vector<float> rapid, cut, lead, plunge, arc;
    std::vector<float> rapidT, leadT, plungeT, cutT, arcT;
    double totalTime = 0;
    float minx = 0, miny = 0, minz = 0, maxx = 0, maxy = 0, maxz = 0;
    bool empty() const { return cut.empty() && rapid.empty() && lead.empty() && plunge.empty() && arc.empty(); }
    void clear() { rapid.clear(); cut.clear(); lead.clear(); plunge.clear(); arc.clear();
                   rapidT.clear(); cutT.clear(); leadT.clear(); plungeT.clear(); arcT.clear();
                   totalTime = 0;
                   minx = miny = minz = maxx = maxy = maxz = 0; }
    // Split every class at time t: `before` gets segments wholly before t (plus the
    // clipped head of a straddling segment), `after` gets the rest.
    void splitAt(double t, CamBackplot& before, CamBackplot& after) const;
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
    // Rapid rates from the tree's machine_detail (fallbacks 5000/3000 mm/min).
    double rapidXY = 5000, rapidZ = 3000;
    void clear() { setups_.clear(); tools_.clear(); bp_.clear(); name_.clear(); revision_ = -1;
                   rapidXY = 5000; rapidZ = 3000; }

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
