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
// class, plus the XY/Z bounds for the in-panel backplot fit.
struct CamBackplot {
    std::vector<float> rapid, cut, lead, plunge, arc;
    float minx = 0, miny = 0, minz = 0, maxx = 0, maxy = 0, maxz = 0;
    bool empty() const { return cut.empty() && rapid.empty() && lead.empty() && plunge.empty() && arc.empty(); }
    void clear() { rapid.clear(); cut.clear(); lead.clear(); plunge.clear(); arc.clear();
                   minx = miny = minz = maxx = maxy = maxz = 0; }
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
    void clear() { setups_.clear(); tools_.clear(); bp_.clear(); name_.clear(); revision_ = -1; }

private:
    int revision_ = -1;
    std::string name_;
    std::string post_;
    std::vector<CamSetupRow> setups_;
    std::vector<CamToolRow> tools_;
    CamBackplot bp_;
};

}  // namespace ui
