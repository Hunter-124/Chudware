// Portable SolidWorks 2015+ saved-display tessellation import.
#include "solidworks_tessellation.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include <miniz.h>

namespace chudware::doc {
namespace {

using Bytes = std::vector<std::uint8_t>;
using StreamMap = std::map<std::string, Bytes>;
using Mat4 = std::array<double, 16>;

constexpr std::array<std::uint8_t, 6> kChunkMarker = {0x14, 0x00, 0x06, 0x00, 0x08, 0x00};
constexpr std::array<std::uint8_t, 12> kStripMarker = {
    0x04, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00};
constexpr std::array<std::uint8_t, 12> kFloatArrayMarker = {
    0x0c, 0x00, 0x00, 0x00, 0x64, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00};
constexpr std::size_t kChunkHeaderSize = 0x1e;
constexpr std::uint32_t kInlineChunk = 65536;
constexpr std::uint32_t kMaxNameBytes = 512;
constexpr std::uint32_t kMaxCompressedBytes = 64u * 1024u * 1024u;
constexpr std::uint32_t kMaxInflatedBytes = 512u * 1024u * 1024u;
constexpr std::uint32_t kMaxArrayItems = 16u * 1024u * 1024u;

// Match the Python portable reader. Keep the existing 512 MiB stream ceiling as the
// whole-file/retained-stream/mesh ceiling too: importing more must use the exact STEP
// lane, not silently simplify engineering geometry. Count logical placed geometry
// even though this reader shares meshes (the native consumer instantiates every body).
struct Limits {
    std::size_t file_bytes = kMaxInflatedBytes;
    std::size_t stream_bytes = kMaxInflatedBytes;
    std::size_t streams = 4096;
    std::size_t instances = 65536;
    std::size_t work = 1024u * 1024u;
    std::size_t vertices = kMaxArrayItems;
    std::size_t triangles = kMaxArrayItems;
    std::size_t mesh_bytes = kMaxInflatedBytes;
};

void charge(std::size_t& used, std::size_t amount, std::size_t limit, const char* kind) {
    if (used > limit || amount > limit - used)
        throw std::runtime_error(std::string("SolidWorks ") + kind + " budget exceeded");
    used += amount;
}

struct MeshBudget {
    std::size_t vertices = 0;
    std::size_t triangles = 0;
    std::size_t bytes = 0;

    void add(std::size_t verts, std::size_t tris, const Limits& limits) {
        charge(vertices, verts, limits.vertices, "aggregate vertex");
        charge(triangles, tris, limits.triangles, "aggregate triangle");
        // Divide before multiplying, so even a malformed count cannot wrap.
        if (verts > limits.mesh_bytes / 24 || tris > limits.mesh_bytes / 12)
            throw std::runtime_error("SolidWorks aggregate mesh byte budget exceeded");
        charge(bytes, verts * 24, limits.mesh_bytes, "aggregate mesh byte");
        charge(bytes, tris * 12, limits.mesh_bytes, "aggregate mesh byte");
    }
};

Mat4 identity_matrix() {
    return {1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0};
}

std::uint32_t read_u32(const Bytes& data, std::size_t offset) {
    if (offset > data.size() || data.size() - offset < 4) return 0;
    return static_cast<std::uint32_t>(data[offset]) |
           (static_cast<std::uint32_t>(data[offset + 1]) << 8u) |
           (static_cast<std::uint32_t>(data[offset + 2]) << 16u) |
           (static_cast<std::uint32_t>(data[offset + 3]) << 24u);
}

float read_f32(const Bytes& data, std::size_t offset) {
    const std::uint32_t bits = read_u32(data, offset);
    float value = 0.0f;
    static_assert(sizeof(value) == sizeof(bits));
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

bool starts_with(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.substr(0, prefix.size()) == prefix;
}

bool matches(const Bytes& data, std::size_t offset,
             const std::array<std::uint8_t, 12>& marker) {
    return offset <= data.size() && data.size() - offset >= marker.size() &&
           std::equal(marker.begin(), marker.end(), data.begin() + static_cast<std::ptrdiff_t>(offset));
}

bool matches_chunk(const Bytes& data, std::size_t offset) {
    return offset <= data.size() && data.size() - offset >= kChunkMarker.size() &&
           std::equal(kChunkMarker.begin(), kChunkMarker.end(),
                      data.begin() + static_cast<std::ptrdiff_t>(offset));
}

Bytes read_file(const std::string& path, const Limits& limits) {
    std::ifstream in(path, std::ios::binary | std::ios::ate);
    if (!in) throw std::runtime_error("cannot open SolidWorks file: " + path);
    const std::streamoff end = in.tellg();
    if (end <= 0) throw std::runtime_error("SolidWorks file is empty: " + path);
    if (static_cast<std::uint64_t>(end) > std::numeric_limits<std::size_t>::max())
        throw std::runtime_error("SolidWorks file is too large to address: " + path);
    if (static_cast<std::uint64_t>(end) > limits.file_bytes)
        throw std::runtime_error("SolidWorks input file byte budget exceeded");
    Bytes data(static_cast<std::size_t>(end));
    in.seekg(0, std::ios::beg);
    if (!in.read(reinterpret_cast<char*>(data.data()), end))
        throw std::runtime_error("failed reading SolidWorks file: " + path);
    return data;
}

std::uint8_t rotate_left(std::uint8_t value, int shift) {
    shift &= 7;
    if (shift == 0) return value;
    return static_cast<std::uint8_t>((value << shift) | (value >> (8 - shift)));
}

std::string decode_stream_name(const Bytes& data, std::size_t offset, std::size_t size, int key) {
    std::string name;
    name.reserve(size);
    for (std::size_t i = 0; i < size; ++i) {
        const std::uint8_t decoded = rotate_left(data[offset + i], key);
        if (decoded < 0x20 || decoded >= 0x80) return {};
        name.push_back(static_cast<char>(decoded));
    }
    return name;
}

bool wanted_stream(std::string_view name, bool assembly) {
    if (!assembly) return name == "Contents/DisplayLists";
    return name == "swXmlContents/COMPINSTANCETREE" ||
           (starts_with(name, "FaceTessellations/") && name != "FaceTessellations/Directory");
}

Bytes inflate_raw(const std::uint8_t* input, std::size_t input_size, std::size_t output_size,
                  const std::string& name) {
    if (output_size == 0 || output_size > kMaxInflatedBytes)
        throw std::runtime_error("SolidWorks stream '" + name + "' has an invalid expanded size");
    Bytes out(output_size);
    const std::size_t got = tinfl_decompress_mem_to_mem(
        out.data(), out.size(), input, input_size, 0);
    if (got == static_cast<std::size_t>(-1) || got != output_size)
        throw std::runtime_error("SolidWorks stream '" + name + "' failed raw-deflate decoding");
    return out;
}

StreamMap read_modern_streams(const std::string& path, bool assembly, const Limits& limits) {
    const Bytes data = read_file(path, limits);
    if (data.size() < 22)
        throw std::runtime_error("SolidWorks file is too short to contain a modern document");
    if (data.size() >= 8 && data[0] == 0xd0 && data[1] == 0xcf && data[2] == 0x11 &&
        data[3] == 0xe0)
        throw std::runtime_error(
            "legacy SolidWorks OLE documents need SolidWorks COM or a STEP export; "
            "portable tessellation import supports SolidWorks 2015 and newer");

    const std::size_t probe_end = std::min<std::size_t>(64, data.size());
    bool modern = false;
    for (std::size_t i = 0; i + kChunkMarker.size() <= probe_end; ++i) {
        if (matches_chunk(data, i)) { modern = true; break; }
    }
    if (!modern)
        throw std::runtime_error(
            "unrecognized SolidWorks container (expected a SolidWorks 2015+ chunk stream)");

    StreamMap streams;
    std::size_t stream_count = 0;
    std::size_t expanded_bytes = 0;
    const int key = data[7];
    std::size_t search = 0;
    while (search + kChunkMarker.size() <= data.size()) {
        std::size_t marker = search;
        while (marker + kChunkMarker.size() <= data.size() && !matches_chunk(data, marker))
            ++marker;
        if (marker + kChunkMarker.size() > data.size()) break;
        if (marker < 4) { search = marker + 1; continue; }
        const std::size_t chunk = marker - 4;
        if (chunk > data.size() || data.size() - chunk < kChunkHeaderSize) break;

        const std::uint32_t f1 = read_u32(data, chunk + 0x0e);
        const std::uint32_t compressed = read_u32(data, chunk + 0x12);
        const std::uint32_t expanded = read_u32(data, chunk + 0x16);
        const std::uint32_t name_size = read_u32(data, chunk + 0x1a);
        if (name_size > kMaxNameBytes || compressed > kMaxCompressedBytes) {
            search = marker + 1;
            continue;
        }
        const std::size_t name_start = chunk + kChunkHeaderSize;
        if (name_start > data.size() || data.size() - name_start < name_size) {
            search = marker + 1;
            continue;
        }
        const std::string name = decode_stream_name(data, name_start, name_size, key);
        if (name.empty()) { search = marker + 1; continue; }
        const std::size_t payload = name_start + name_size;
        if (f1 < kInlineChunk) { search = marker + kChunkMarker.size(); continue; }
        if (compressed == 0) {
            if (wanted_stream(name, assembly) && streams.find(name) == streams.end()) {
                charge(stream_count, 1, limits.streams, "stream count");
                streams.try_emplace(name, Bytes{});
            }
            search = marker + kChunkMarker.size();
            continue;
        }
        if (payload > data.size() || data.size() - payload < compressed) {
            search = marker + 1;
            continue;
        }
        if (wanted_stream(name, assembly) && streams.find(name) == streams.end()) {
            if (expanded == 0 || expanded > kMaxInflatedBytes)
                throw std::runtime_error("SolidWorks stream '" + name + "' has an invalid expanded size");
            charge(stream_count, 1, limits.streams, "stream count");
            charge(expanded_bytes, expanded, limits.stream_bytes, "aggregate expanded byte");
            streams.emplace(name, inflate_raw(data.data() + payload, compressed, expanded, name));
        }
        search = payload + compressed;
    }
    return streams;
}

struct ArrayRecord {
    std::size_t offset = 0;
    std::uint32_t count = 0;
};

std::vector<ArrayRecord> float_arrays(const Bytes& data) {
    std::vector<ArrayRecord> candidates;
    for (std::size_t p = 0; p + 16 <= data.size(); ++p) {
        if (!matches(data, p, kFloatArrayMarker)) continue;
        const std::uint32_t count = read_u32(data, p + 12);
        if (count > kMaxArrayItems) continue;
        const std::uint64_t bytes = static_cast<std::uint64_t>(count) * 12u;
        if (bytes > data.size() - (p + 16)) continue;
        candidates.push_back({p, count});
    }

    std::vector<ArrayRecord> grouped;
    for (std::size_t i = 0; i + 3 < candidates.size();) {
        const auto& a = candidates[i];
        const auto& n = candidates[i + 1];
        const auto& uv = candidates[i + 2];
        const auto& extra = candidates[i + 3];
        const std::size_t a_end = a.offset + 16 + static_cast<std::size_t>(a.count) * 12;
        const std::size_t n_end = n.offset + 16 + static_cast<std::size_t>(n.count) * 12;
        if (a.count == n.count && uv.count == 0 && extra.count == 0 &&
            a_end <= n.offset && n_end <= uv.offset && uv.offset < extra.offset) {
            grouped.insert(grouped.end(), {a, n, uv, extra});
            i += 4;
        } else {
            ++i;
        }
    }
    return grouped;
}

std::map<std::size_t, std::size_t> strip_headers(const Bytes& data) {
    std::map<std::size_t, std::size_t> by_end;
    for (std::size_t p = 0; p + 16 <= data.size(); ++p) {
        if (!matches(data, p, kStripMarker)) continue;
        const std::uint32_t count = read_u32(data, p + 12);
        if (count > kMaxArrayItems) continue;
        const std::uint64_t end = static_cast<std::uint64_t>(p) + 16u +
                                  static_cast<std::uint64_t>(count) * 4u;
        if (end <= data.size()) by_end[static_cast<std::size_t>(end)] = p;
    }
    return by_end;
}

MeshData parse_face_tessellations(const Bytes& data, std::optional<std::uint32_t> expected_faces,
                                 MeshBudget& budget, const Limits& limits) {
    const std::vector<ArrayRecord> arrays = float_arrays(data);
    if (arrays.empty() || arrays.size() % 4 != 0)
        throw std::runtime_error("saved SolidWorks display list contains no complete face arrays");
    const std::size_t face_count = arrays.size() / 4;
    if (expected_faces && face_count != *expected_faces)
        throw std::runtime_error("SolidWorks tessellation face-count mismatch: header says " +
                                 std::to_string(*expected_faces) + ", decoded " +
                                 std::to_string(face_count));
    const auto headers = strip_headers(data);

    MeshData mesh;
    std::uint64_t total_vertices = 0;
    for (std::size_t face = 0; face < face_count; ++face)
        total_vertices += arrays[face * 4].count;
    if (total_vertices > std::numeric_limits<std::uint32_t>::max())
        throw std::runtime_error("SolidWorks tessellation has more than 2^32 vertices");
    budget.add(static_cast<std::size_t>(total_vertices), 0, limits);
    mesh.verts.reserve(static_cast<std::size_t>(total_vertices) * 3);

    for (std::size_t face = 0; face < face_count; ++face) {
        const ArrayRecord& positions = arrays[face * 4];
        const auto header_it = headers.find(positions.offset);
        if (header_it == headers.end())
            throw std::runtime_error("SolidWorks face is missing its triangle-strip table");
        const std::size_t header = header_it->second;
        const std::uint32_t strip_count = read_u32(data, header + 12);
        std::uint64_t strip_vertices = 0;
        for (std::uint32_t i = 0; i < strip_count; ++i)
            strip_vertices += read_u32(data, header + 16 + static_cast<std::size_t>(i) * 4);
        if (strip_vertices != positions.count)
            throw std::runtime_error("SolidWorks triangle-strip lengths do not cover the face");

        const std::uint32_t base = static_cast<std::uint32_t>(mesh.vertex_count());
        const std::size_t position_data = positions.offset + 16;
        for (std::uint32_t i = 0; i < positions.count; ++i) {
            const std::size_t p = position_data + static_cast<std::size_t>(i) * 12;
            // SolidWorks display coordinates are metres; Chudware/OCCT coordinates are mm.
            mesh.verts.push_back(static_cast<double>(read_f32(data, p)) * 1000.0);
            mesh.verts.push_back(static_cast<double>(read_f32(data, p + 4)) * 1000.0);
            mesh.verts.push_back(static_cast<double>(read_f32(data, p + 8)) * 1000.0);
        }

        // The second array of the group is SolidWorks' per-vertex NORMAL array. It is
        // deliberately NOT read: on real parts it is garbage (a turned spacer authored
        // (0,0,±1) for all 2208 vertices; another part's signs disagreed with the triangle
        // winding on 1040 of 1176 triangles), so shading it produced a worse picture than
        // no normals at all. mesh_to_shape derives smooth normals from the triangle soup
        // instead. The group's four-array stride is still what locates the positions.

        std::uint32_t cursor = 0;
        for (std::uint32_t strip = 0; strip < strip_count; ++strip) {
            const std::uint32_t length = read_u32(
                data, header + 16 + static_cast<std::size_t>(strip) * 4);
            for (std::uint32_t j = 0; j + 2 < length; ++j) {
                std::uint32_t ia = base + cursor + j;
                std::uint32_t ib = base + cursor + j + 1;
                const std::uint32_t ic = base + cursor + j + 2;
                if (j & 1u) std::swap(ia, ib);
                const auto point = [&](std::uint32_t idx, int axis) {
                    return mesh.verts[static_cast<std::size_t>(idx) * 3 + axis];
                };
                const double abx = point(ib, 0) - point(ia, 0);
                const double aby = point(ib, 1) - point(ia, 1);
                const double abz = point(ib, 2) - point(ia, 2);
                const double acx = point(ic, 0) - point(ia, 0);
                const double acy = point(ic, 1) - point(ia, 1);
                const double acz = point(ic, 2) - point(ia, 2);
                const double cx = aby * acz - abz * acy;
                const double cy = abz * acx - abx * acz;
                const double cz = abx * acy - aby * acx;
                if (cx * cx + cy * cy + cz * cz <= 1e-20) continue;
                budget.add(0, 1, limits);
                mesh.tris.insert(mesh.tris.end(), {ia, ib, ic});
            }
            cursor += length;
        }
    }
    if (mesh.triangle_count() == 0)
        throw std::runtime_error("saved SolidWorks display list contains no non-degenerate triangles");
    return mesh;
}

std::vector<std::pair<std::size_t, std::size_t>> assembly_blocks(const Bytes& data) {
    std::vector<std::size_t> starts;
    for (std::size_t marker = 56; marker + 16 <= data.size(); ++marker) {
        if (!matches(data, marker, kStripMarker)) continue;
        const std::size_t candidate = marker - 56;
        if (read_u32(data, candidate) != starts.size() ||
            read_u32(data, candidate + 24) != 1 ||
            read_u32(data, candidate + 28) != 0 ||
            read_u32(data, candidate + 32) != 0 ||
            read_u32(data, candidate + 40) != 0 ||
            read_u32(data, candidate + 44) != 0 ||
            read_u32(data, candidate + 48) != 0 ||
            read_u32(data, candidate + 52) != 0)
            continue;
        const std::uint32_t faces = read_u32(data, candidate + 12);
        if (faces == 0 || faces > kMaxArrayItems) continue;
        starts.push_back(candidate);
    }
    std::vector<std::pair<std::size_t, std::size_t>> blocks;
    for (std::size_t i = 0; i < starts.size(); ++i)
        blocks.emplace_back(starts[i], i + 1 < starts.size() ? starts[i + 1] : data.size());
    return blocks;
}

std::string xml_unescape(std::string value) {
    const std::array<std::pair<std::string_view, std::string_view>, 5> entities = {{
        {"&quot;", "\""}, {"&apos;", "'"}, {"&lt;", "<"}, {"&gt;", ">"}, {"&amp;", "&"}}};
    for (const auto& [encoded, decoded] : entities) {
        std::size_t p = 0;
        while ((p = value.find(encoded, p)) != std::string::npos) {
            value.replace(p, encoded.size(), decoded);
            p += decoded.size();
        }
    }
    return value;
}

using Attributes = std::unordered_map<std::string, std::string>;

Attributes parse_attributes(std::string_view text) {
    Attributes out;
    std::size_t p = 0;
    while (p < text.size()) {
        while (p < text.size() && std::isspace(static_cast<unsigned char>(text[p]))) ++p;
        const std::size_t key_start = p;
        while (p < text.size() && !std::isspace(static_cast<unsigned char>(text[p])) &&
               text[p] != '=') ++p;
        if (p == key_start) { ++p; continue; }
        std::string key(text.substr(key_start, p - key_start));
        while (p < text.size() && std::isspace(static_cast<unsigned char>(text[p]))) ++p;
        if (p >= text.size() || text[p] != '=') continue;
        ++p;
        while (p < text.size() && std::isspace(static_cast<unsigned char>(text[p]))) ++p;
        if (p >= text.size() || (text[p] != '\'' && text[p] != '\"')) continue;
        const char quote = text[p++];
        const std::size_t value_start = p;
        while (p < text.size() && text[p] != quote) ++p;
        if (p >= text.size()) break;
        out.emplace(std::move(key), xml_unescape(std::string(text.substr(value_start, p - value_start))));
        ++p;
    }
    return out;
}

struct Reference {
    std::string model_ref;
    std::string name;
    std::string number;
    bool suppressed = false;
    Mat4 transform = identity_matrix();
};

struct Model {
    std::string id;
    std::string name;
    std::string file_ref;
    std::string doc_type;
    std::vector<Reference> references;
};

struct AssemblyTree {
    std::vector<Model> models;
    std::unordered_map<std::string, std::size_t> model_index;
    std::string root_model;
};

Mat4 parse_matrix(const std::string& value) {
    Mat4 out{};
    std::istringstream in(value);
    for (double& item : out) {
        if (!(in >> item)) throw std::runtime_error("SolidWorks component has a malformed transform");
    }
    double extra = 0.0;
    if (in >> extra) throw std::runtime_error("SolidWorks component transform has more than 16 values");
    return out;
}

AssemblyTree parse_assembly_xml(const Bytes& bytes) {
    const std::string xml(bytes.begin(), bytes.end());
    AssemblyTree tree;
    std::unordered_map<std::string, std::string> file_types;
    std::vector<std::string> model_stack;
    std::string first_configuration;

    std::size_t p = 0;
    while ((p = xml.find('<', p)) != std::string::npos) {
        const std::size_t end = xml.find('>', p + 1);
        if (end == std::string::npos) break;
        std::string_view tag(xml.data() + p + 1, end - p - 1);
        p = end + 1;
        if (tag.empty() || tag.front() == '?' || tag.front() == '!') continue;
        bool closing = false;
        if (tag.front() == '/') { closing = true; tag.remove_prefix(1); }
        while (!tag.empty() && std::isspace(static_cast<unsigned char>(tag.front())))
            tag.remove_prefix(1);
        bool self_closing = false;
        while (!tag.empty() && std::isspace(static_cast<unsigned char>(tag.back())))
            tag.remove_suffix(1);
        if (!tag.empty() && tag.back() == '/') {
            self_closing = true;
            tag.remove_suffix(1);
            while (!tag.empty() && std::isspace(static_cast<unsigned char>(tag.back())))
                tag.remove_suffix(1);
        }
        const std::size_t split = tag.find_first_of(" \t\r\n");
        const std::string name(tag.substr(0, split));
        if (closing) {
            if (name == "swModel" && !model_stack.empty()) model_stack.pop_back();
            continue;
        }
        const Attributes attrs = split == std::string_view::npos
                                     ? Attributes{}
                                     : parse_attributes(tag.substr(split + 1));
        const auto get = [&](std::string_view key) -> std::string {
            const auto it = attrs.find(std::string(key));
            return it == attrs.end() ? std::string() : it->second;
        };

        if (name == "swFile") {
            const std::string id = get("id");
            if (!id.empty()) file_types[id] = get("swDocType");
        } else if (name == "swModel") {
            Model model;
            model.id = get("id");
            model.name = get("swName");
            model.file_ref = get("swFileRef");
            if (model.id.empty()) throw std::runtime_error("SolidWorks assembly model has no id");
            tree.model_index[model.id] = tree.models.size();
            tree.models.push_back(std::move(model));
            if (!self_closing) model_stack.push_back(tree.models.back().id);
        } else if (name == "swReference") {
            if (model_stack.empty())
                throw std::runtime_error("SolidWorks component reference has no owning model");
            Reference ref;
            ref.model_ref = get("swModelRef");
            ref.name = get("swName");
            ref.number = get("swReferenceNumber");
            ref.suppressed = get("swSuppressed") == "YES";
            ref.transform = parse_matrix(get("swTransform"));
            tree.models.at(tree.model_index.at(model_stack.back())).references.push_back(std::move(ref));
        } else if (name == "swConfiguration") {
            const std::string model_ref = get("swModelRef");
            if (first_configuration.empty()) first_configuration = model_ref;
            if (get("swMostRecentConfiguration") == "YES") tree.root_model = model_ref;
        }
    }

    for (Model& model : tree.models) {
        const auto it = file_types.find(model.file_ref);
        if (it != file_types.end()) model.doc_type = it->second;
    }
    if (tree.root_model.empty()) tree.root_model = first_configuration;
    if (tree.root_model.empty() || tree.model_index.find(tree.root_model) == tree.model_index.end())
        throw std::runtime_error("SolidWorks assembly has no resolvable root configuration");
    return tree;
}

Mat4 multiply(const Mat4& a, const Mat4& b) {
    Mat4 out{};
    for (int row = 0; row < 4; ++row)
        for (int col = 0; col < 4; ++col)
            for (int k = 0; k < 4; ++k)
                out[static_cast<std::size_t>(row) * 4 + col] +=
                    a[static_cast<std::size_t>(row) * 4 + k] *
                    b[static_cast<std::size_t>(k) * 4 + col];
    return out;
}

std::string unique_name(std::string candidate, std::unordered_map<std::string, int>& used) {
    if (candidate.empty()) candidate = "Component";
    const int count = ++used[candidate];
    return count == 1 ? candidate : candidate + "-" + std::to_string(count);
}

// Memoized costs account for every DAG path without materializing instances. Heights
// retain the depth guard when a model is reached later through a longer path.
struct ExpansionCost {
    std::size_t instances = 0;
    std::size_t work = 0;
    MeshBudget mesh;
    int height = 0;
};

ExpansionCost expansion_cost(
    const AssemblyTree& tree,
    const std::unordered_map<std::string, std::shared_ptr<const MeshData>>& meshes,
    const Limits& limits) {
    std::unordered_map<std::string, ExpansionCost> memo;
    std::unordered_set<std::string> stack;
    const auto count = [&](auto&& self, const std::string& id, int depth) -> ExpansionCost {
        if (depth > 64) throw std::runtime_error("SolidWorks assembly nesting exceeds 64 levels");
        const auto cached = memo.find(id);
        if (cached != memo.end()) {
            if (depth + cached->second.height > 64)
                throw std::runtime_error("SolidWorks assembly nesting exceeds 64 levels");
            return cached->second;
        }
        if (!stack.insert(id).second)
            throw std::runtime_error("SolidWorks assembly component graph contains a cycle");
        const auto owner = tree.model_index.find(id);
        if (owner == tree.model_index.end())
            throw std::runtime_error("SolidWorks assembly references unknown model id " + id);
        ExpansionCost cost;
        for (const Reference& ref : tree.models[owner->second].references) {
            charge(cost.work, 1, limits.work, "instance work");
            if (ref.suppressed) continue;
            const auto mesh = meshes.find(ref.model_ref);
            if (mesh != meshes.end()) {
                charge(cost.instances, 1, limits.instances, "instance count");
                cost.mesh.add(mesh->second->vertex_count(), mesh->second->triangle_count(), limits);
            } else {
                const ExpansionCost child = self(self, ref.model_ref, depth + 1);
                charge(cost.instances, child.instances, limits.instances, "instance count");
                charge(cost.work, child.work, limits.work, "instance work");
                cost.mesh.add(child.mesh.vertices, child.mesh.triangles, limits);
                cost.height = std::max(cost.height, child.height + 1);
            }
        }
        stack.erase(id);
        memo.emplace(id, cost);
        return cost;
    };
    return count(count, tree.root_model, 0);
}

std::vector<SolidWorksTessellationBody> read_solidworks_tessellation_impl(
    const std::string& path, bool assembly, const Limits& limits) {
    StreamMap streams = read_modern_streams(path, assembly, limits);
    MeshBudget source_budget;
    if (!assembly) {
        const auto it = streams.find("Contents/DisplayLists");
        if (it == streams.end() || it->second.empty())
            throw std::runtime_error(
                "SolidWorks part has no saved display tessellation; rebuild/save it in SolidWorks "
                "or export STEP");
        std::size_t instance_count = 0;
        charge(instance_count, 1, limits.instances, "instance count");
        auto mesh = std::make_shared<MeshData>(
            parse_face_tessellations(it->second, std::nullopt, source_budget, limits));
        std::string name = std::filesystem::path(path).stem().string();
        return {{name.empty() ? std::string("SolidWorks Part") : std::move(name),
                 std::move(mesh), identity_matrix()}};
    }

    const auto xml_it = streams.find("swXmlContents/COMPINSTANCETREE");
    if (xml_it == streams.end() || xml_it->second.empty())
        throw std::runtime_error(
            "SolidWorks assembly has no component tree; rebuild/save it resolved in SolidWorks");
    AssemblyTree tree = parse_assembly_xml(xml_it->second);

    std::vector<std::shared_ptr<const MeshData>> block_meshes;
    for (const auto& [name, bytes] : streams) {
        if (!starts_with(name, "FaceTessellations/")) continue;
        const auto blocks = assembly_blocks(bytes);
        if (blocks.empty())
            throw std::runtime_error("SolidWorks assembly face stream '" + name +
                                     "' contains no component blocks");
        for (const auto& [start, end] : blocks) {
            std::size_t count = block_meshes.size();
            charge(count, 1, limits.instances, "source mesh count");
            Bytes block(bytes.begin() + static_cast<std::ptrdiff_t>(start),
                        bytes.begin() + static_cast<std::ptrdiff_t>(end));
            const std::uint32_t expected_faces = read_u32(block, 12);
            block_meshes.push_back(std::make_shared<MeshData>(
                parse_face_tessellations(block, expected_faces, source_budget, limits)));
        }
    }
    if (block_meshes.empty())
        throw std::runtime_error(
            "SolidWorks assembly has no saved face tessellations; resolve/rebuild/save it in "
            "SolidWorks or export STEP");

    std::unordered_set<std::string> active_part_models;
    for (const Model& owner : tree.models)
        for (const Reference& ref : owner.references)
            if (!ref.suppressed) {
                const auto it = tree.model_index.find(ref.model_ref);
                if (it != tree.model_index.end() && tree.models[it->second].doc_type == "PART")
                    active_part_models.insert(ref.model_ref);
            }
    std::vector<const Model*> part_models;
    for (const Model& model : tree.models)
        if (model.doc_type == "PART") part_models.push_back(&model);
    if (part_models.size() != block_meshes.size()) {
        std::vector<const Model*> active;
        for (const Model* model : part_models)
            if (active_part_models.find(model->id) != active_part_models.end())
                active.push_back(model);
        if (active.size() == block_meshes.size())
            part_models = std::move(active);
        else
            throw std::runtime_error(
                "SolidWorks assembly component/tessellation mismatch: " +
                std::to_string(part_models.size()) + " part models but " +
                std::to_string(block_meshes.size()) + " saved component meshes");
    }
    std::unordered_map<std::string, std::shared_ptr<const MeshData>> mesh_by_model;
    for (std::size_t i = 0; i < part_models.size(); ++i)
        mesh_by_model.emplace(part_models[i]->id, block_meshes[i]);

    const ExpansionCost expected = expansion_cost(tree, mesh_by_model, limits);
    std::size_t instance_count = 0;
    std::size_t work = 0;
    MeshBudget placed_budget;

    std::vector<SolidWorksTessellationBody> out;
    out.reserve(expected.instances);
    std::unordered_map<std::string, int> used_names;
    std::unordered_set<std::string> recursion_stack;
    const auto expand = [&](auto&& self, const std::string& model_id, const Mat4& parent,
                            const std::string& prefix, int depth) -> void {
        if (depth > 64) throw std::runtime_error("SolidWorks assembly nesting exceeds 64 levels");
        if (!recursion_stack.insert(model_id).second)
            throw std::runtime_error("SolidWorks assembly component graph contains a cycle");
        const auto owner_it = tree.model_index.find(model_id);
        if (owner_it == tree.model_index.end())
            throw std::runtime_error("SolidWorks assembly references unknown model id " + model_id);
        const Model& owner = tree.models[owner_it->second];
        for (const Reference& ref : owner.references) {
            charge(work, 1, limits.work, "instance work");
            if (ref.suppressed) continue;
            const Mat4 world = multiply(ref.transform, parent);  // row-vector local * parent
            const auto mesh_it = mesh_by_model.find(ref.model_ref);
            const std::string local_name = ref.name +
                (ref.number.empty() ? std::string() : "-" + ref.number);
            const std::string path_name = prefix.empty() ? local_name : prefix + "/" + local_name;
            if (mesh_it != mesh_by_model.end()) {
                charge(instance_count, 1, limits.instances, "instance count");
                placed_budget.add(mesh_it->second->vertex_count(),
                                  mesh_it->second->triangle_count(), limits);
                out.push_back({unique_name(path_name, used_names), mesh_it->second, world});
            } else {
                self(self, ref.model_ref, world, path_name, depth + 1);
            }
        }
        recursion_stack.erase(model_id);
    };
    expand(expand, tree.root_model, identity_matrix(), std::string(), 0);
    if (out.empty())
        throw std::runtime_error("SolidWorks assembly resolved no unsuppressed part instances");
    return out;
}

}  // namespace

std::vector<SolidWorksTessellationBody> read_solidworks_tessellation(
    const std::string& path, bool assembly) {
    return read_solidworks_tessellation_impl(path, assembly, Limits{});
}

}  // namespace chudware::doc
