// Triangle mesh + binary STL loader with smooth-normal welding.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "vec_math.h"

struct Mesh {
    std::vector<float> positions;  // xyz per welded vertex
    std::vector<float> normals;    // xyz per welded vertex (smoothed)
    std::vector<uint32_t> indices; // triangle indices into positions/normals
    std::vector<float> edgeLines;  // feature edges as line list: 6 floats (2 xyz) per edge
    Vec3 bbMin, bbMax, center;
    float radius = 1.0f;           // bounding-sphere radius about center

    size_t triangleCount() const { return indices.size() / 3; }
    bool empty() const { return indices.empty(); }
};

// Load a binary STL. Welds coincident vertices and averages face normals so curved
// surfaces shade smoothly. Returns false on read/parse failure.
bool loadBinarySTL(const std::string& path, Mesh& out, std::string& err);
