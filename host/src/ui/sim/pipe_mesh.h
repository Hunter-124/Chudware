#pragma once
#include "sim_scene.h"
#include <nlohmann/json.hpp>
#include <string>
#include <cmath>

namespace ui { class PipingPanel; }
namespace ui::pipescene {
struct Point {
    float x = 0, y = 0, z = 0;
    Point operator+(Point b) const { return {x+b.x,y+b.y,z+b.z}; }
    Point operator-(Point b) const { return {x-b.x,y-b.y,z-b.z}; }
    Point operator*(float s) const { return {x*s,y*s,z*s}; }
};
inline float dot(Point a, Point b) { return a.x*b.x+a.y*b.y+a.z*b.z; }
inline Point cross(Point a, Point b) { return {a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x}; }
inline float length(Point a) { return std::sqrt(dot(a,a)); }
inline Point unit(Point a) { const float l=length(a); return l>1e-7f ? a*(1/l) : Point{1,0,0}; }
inline bool project(const simscene::ViewportCtx& vp, Point p, ImVec2& s) {
    const float xyz[]={p.x,p.y,p.z}; return simscene::worldToScreen(vp,xyz,s);
}
struct Soup {
    std::vector<float> xyz, nrm;
    void clear() { xyz.clear(); nrm.clear(); }
};
struct RunGeometry {
    std::string id;
    std::vector<Point> path, rounded;
    float od=60.3f, bendRadius=90.45f;
    std::size_t first=0, end=0;
    float pressure0=0, pressure1=0, velocity=0;
    bool solved=false;
};
struct FittingGeometry {
    std::string id, type, runId;
    Point origin, axis{1,0,0}, branch{0,1,0};
    float od=60.3f, od2=60.3f;
    nlohmann::json params;
    std::size_t first=0, end=0;
    bool bendCovered=false;
};
float outerDiameter(double nps);
void appendRun(Soup& mesh, const std::vector<Point>& path, float od, float bendRadius,
               std::vector<Point>* rounded=nullptr);
void appendFitting(Soup& mesh, const FittingGeometry& fitting);
struct MeshCache {
    std::vector<RunGeometry> runs;
    std::vector<FittingGeometry> fittings;
    Soup mesh;
    std::vector<float> rgb, centerline, pressureFraction;
    std::uint64_t revision=1, lineRevision=1, networkRevision=0;
    const PipingPanel* owner=nullptr;
    const void* solveLinks=nullptr;
    const void* solveNodes=nullptr;
    int colorBy=-1;
    std::string selectedRun, selectedFitting, hoverRun, hoverFitting;
    std::string lastHoverRun, lastHoverFitting;
    float paletteKey[9]{};
    void update(PipingPanel& panel);
    void collect(simscene::Scene& out) const;
};
struct Hit {
    int run=-1, fitting=-1;
    Point point;
    float distance=0;
};
Hit hitRun(const MeshCache& mesh, const simscene::ViewportCtx& vp, ImVec2 mouse,
           float threshold, bool vertices=false, bool rounded=false);
Hit hitFitting(const MeshCache& mesh, const simscene::ViewportCtx& vp, ImVec2 mouse,
               float threshold);
} // namespace ui::pipescene
