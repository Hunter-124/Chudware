// GL-free decoding of bounded, revision-pinned mesh-reference pages.
#pragma once

#include "mesh.h"
#include <nlohmann/json.hpp>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace render {
inline std::string meshReferenceKey(const nlohmann::json& ref) {
    if (!ref.is_object() || !ref.contains("transform")) return {};
    const auto hash = ref.find("sha256");
    if (hash == ref.end() || !hash->is_string() || hash->get_ref<const std::string&>().empty()) return {};
    return hash->get_ref<const std::string&>() + ":" + ref["transform"].dump();
}

// Each page has its own local indices, so its normals can be computed independently. Appending
// one page at a time keeps the GUI responsive even for a scan with millions of triangles.
inline void appendMeshReferencePage(Mesh& mesh, const nlohmann::json& page,
                                    size_t& offset, size_t total) {
    auto positions = page.at("vertices_mm").get<std::vector<float>>();
    auto indices = page.at("indices").get<std::vector<uint32_t>>();
    if (offset > total || positions.size() % 3 || indices.empty() || indices.size() % 3 ||
        indices.size() > 30000 || positions.size() > indices.size() * 3 ||
        page.at("offset_tris").get<size_t>() != offset ||
        page.at("total_tris").get<size_t>() != total ||
        indices.size() / 3 > total - offset ||
        mesh.positions.size() / 3 + positions.size() / 3 > std::numeric_limits<uint32_t>::max())
        throw std::runtime_error("invalid reference mesh page");
    for (float value : positions)
        if (!std::isfinite(value)) throw std::runtime_error("non-finite reference vertex");
    for (uint32_t index : indices)
        if (index >= positions.size() / 3) throw std::runtime_error("invalid reference index");
    const size_t nextOffset = offset + indices.size() / 3;
    const auto& next = page.at("next_offset_tris");
    if ((nextOffset == total && !next.is_null()) ||
        (nextOffset < total && (next.is_null() || next.get<size_t>() != nextOffset)))
        throw std::runtime_error("invalid reference page continuation");

    std::vector<float> normals(positions.size(), 0.0f);
    for (size_t i = 0; i < indices.size(); i += 3) {
        const size_t a = 3 * size_t(indices[i]);
        const size_t b = 3 * size_t(indices[i + 1]);
        const size_t c = 3 * size_t(indices[i + 2]);
        const float ux = positions[b] - positions[a], uy = positions[b + 1] - positions[a + 1];
        const float uz = positions[b + 2] - positions[a + 2];
        const float vx = positions[c] - positions[a], vy = positions[c + 1] - positions[a + 1];
        const float vz = positions[c + 2] - positions[a + 2];
        const float nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx;
        for (size_t p : {a, b, c}) {
            normals[p] += nx; normals[p + 1] += ny; normals[p + 2] += nz;
        }
    }
    for (size_t i = 0; i < normals.size(); i += 3) {
        const float x = normals[i], y = normals[i + 1], z = normals[i + 2];
        const float length = std::sqrt(x * x + y * y + z * z);
        if (length > 1e-20f && std::isfinite(length)) {
            normals[i] /= length; normals[i + 1] /= length; normals[i + 2] /= length;
        } else {
            normals[i] = 0; normals[i + 1] = 0; normals[i + 2] = 1;
        }
    }
    const uint32_t base = static_cast<uint32_t>(mesh.positions.size() / 3);
    for (uint32_t& index : indices) index += base;
    mesh.positions.insert(mesh.positions.end(), positions.begin(), positions.end());
    mesh.normals.insert(mesh.normals.end(), normals.begin(), normals.end());
    mesh.indices.insert(mesh.indices.end(), indices.begin(), indices.end());
    offset = nextOffset;
}
}  // namespace render
