// Shared PLY implementation for native import and standalone sanitizer regressions.
// Include inside chudware::doc with MeshData, OpError and standard parsing headers available.
// ---- PLY reader: ASCII + binary_little_endian (trimesh's default writer) -----
// OCCT 7.9.3 has no PLY *reader* (TKDESTL ships only RWPly_CafWriter). The oracle
// (trimesh) writes binary_little_endian PLY by default, so the C++ reader must handle
// both encodings. We parse the standard layout: a `vertex` element with float x/y/z
// (plus possible extra per-vertex props we skip by byte-size) and a `face` element with
// a `list <countT> <idxT> vertex_indices` property. Big-endian + non-standard layouts
// are deferred (raise import_failed) — trimesh never emits them for our meshes.
namespace ply {

struct Prop {
    std::string name;
    std::string type;      // scalar type (or list-value type for a list prop)
    bool is_list = false;
    std::string count_type;  // list count scalar type
};

constexpr std::size_t kMaxElements = 10000000;
constexpr std::size_t kMaxListItems = 1000000;
constexpr std::size_t kMaxTotalListItems = 30000000;
constexpr std::size_t kMaxInputBytes = std::size_t{512} << 20;

struct ScalarType {
    std::size_t width;
    bool integral;
    bool signed_value;
};

ScalarType scalar_type(const std::string& t) {
    if (t == "char" || t == "int8") return {1, true, true};
    if (t == "uchar" || t == "uint8") return {1, true, false};
    if (t == "short" || t == "int16") return {2, true, true};
    if (t == "ushort" || t == "uint16") return {2, true, false};
    if (t == "int" || t == "int32") return {4, true, true};
    if (t == "uint" || t == "uint32") return {4, true, false};
    if (t == "int64") return {8, true, true};
    if (t == "uint64") return {8, true, false};
    if (t == "float" || t == "float32") return {4, false, true};
    if (t == "double" || t == "float64") return {8, false, true};
    throw OpError("import_failed", "PLY: unsupported property type '" + t + "'");
}

// Every scalar, including skipped properties and list counts, owns its complete extent.
double read_le_scalar(const unsigned char*& p, const unsigned char* end, const std::string& t) {
    const ScalarType type = scalar_type(t);
    if (static_cast<std::size_t>(end - p) < type.width)
        throw OpError("import_failed", "PLY: truncated binary scalar");
    uint64_t bits = 0;
    for (std::size_t i = 0; i < type.width; ++i)
        bits |= static_cast<uint64_t>(p[i]) << (8 * i);
    p += type.width;
    if (type.integral) {
        if (!type.signed_value) return static_cast<double>(bits);
        // Sign extend without overflowing signed arithmetic or an out-of-range cast.
        const uint64_t sign = uint64_t{1} << (type.width * 8 - 1);
        if ((bits & sign) == 0) return static_cast<double>(bits);
        const uint64_t mask = type.width == 8 ? UINT64_MAX : (sign * 2 - 1);
        return -static_cast<double>((~bits & mask) + 1);
    }
    if (type.width == 4) {
        const uint32_t b = static_cast<uint32_t>(bits);
        float f;
        std::memcpy(&f, &b, sizeof(f));
        return f;
    }
    double d;
    std::memcpy(&d, &bits, sizeof(d));
    return d;
}

double read_ascii_scalar(std::istringstream& input, const std::string& t) {
    std::string token;
    if (!(input >> token)) throw OpError("import_failed", "PLY: truncated ASCII property");
    const ScalarType type = scalar_type(t);
    if (type.integral) {
        const char* begin = token.data();
        const char* finish = begin + token.size();
        if (begin != finish && *begin == '+') ++begin;
        bool valid = false;
        double integer = 0;
        if (type.signed_value) {
            int64_t number = 0;
            const auto parsed = std::from_chars(begin, finish, number);
            valid = parsed.ec == std::errc{} && parsed.ptr == finish;
            if (type.width < 8) {
                const int64_t bound = int64_t{1} << (type.width * 8 - 1);
                valid = valid && number >= -bound && number < bound;
            }
            integer = static_cast<double>(number);
        } else {
            uint64_t number = 0;
            const auto parsed = std::from_chars(begin, finish, number);
            valid = parsed.ec == std::errc{} && parsed.ptr == finish;
            if (type.width < 8) valid = valid && number < (uint64_t{1} << (type.width * 8));
            integer = static_cast<double>(number);
        }
        if (!valid) throw OpError("import_failed", "PLY: integer property outside its declared type");
        return integer;
    }
    char* end = nullptr;
    const double value = std::strtod(token.c_str(), &end);
    if (end == token.c_str() || end != token.c_str() + token.size())
        throw OpError("import_failed", "PLY: invalid ASCII scalar");
    return value;
}

}  // namespace ply

MeshData read_ply(const std::string& path) {
    std::ifstream is(path, std::ios::in | std::ios::binary);
    if (!is) throw OpError("import_failed", "PLY: cannot open input", {{"path", path}});
    auto reject = [&](const std::string& message) {
        throw OpError("import_failed", "PLY: " + message, {{"path", path}});
    };
    is.seekg(0, std::ios::end);
    const auto file_end = is.tellg();
    if (file_end < 0 || static_cast<uint64_t>(file_end) > ply::kMaxInputBytes)
        reject("input exceeds byte budget");
    is.seekg(0, std::ios::beg);
    std::string line;
    std::getline(is, line);
    if (line != "ply" && line != "ply\r") reject("missing magic header");
    enum Enc { ASCII, BIN_LE } enc = ASCII;
    bool have_format = false, have_vertex = false, have_face = false, have_end = false;
    std::size_t n_vert = 0, n_face = 0, header_end = 0;
    std::vector<ply::Prop> vprops, fprops;
    enum { NONE, VERTEX, FACE } cur = NONE;
    while (std::getline(is, line)) {
        if (is.tellg() < 0 || is.tellg() > std::streamoff{1 << 20})
            reject("header exceeds byte budget or is unterminated");
        std::istringstream ls(line);
        std::string tok;
        ls >> tok;
        if (tok == "format") {
            std::string f, version;
            if (!(ls >> f >> version) || version != "1.0" || have_format)
                reject("invalid format declaration");
            have_format = true;
            if (f == "ascii") enc = ASCII;
            else if (f == "binary_little_endian") enc = BIN_LE;
            else reject("unsupported encoding '" + f + "'");
        } else if (tok == "element") {
            std::string e;
            int64_t n = -1;
            if (!(ls >> e >> n) || n < 0 || static_cast<uint64_t>(n) > ply::kMaxElements)
                reject("invalid or over-budget element count");
            if (e == "vertex") {
                if (have_vertex || have_face) reject("unsupported vertex element ordering");
                have_vertex = true;
                n_vert = static_cast<std::size_t>(n);
                cur = VERTEX;
            } else if (e == "face") {
                if (!have_vertex || have_face) reject("unsupported face element ordering");
                have_face = true;
                n_face = static_cast<std::size_t>(n);
                cur = FACE;
            } else {
                if (n != 0) reject("unsupported nonempty element '" + e + "'");
                cur = NONE;
            }
        } else if (tok == "property" && cur != NONE) {
            ply::Prop pr;
            std::string t;
            if (!(ls >> t)) reject("invalid property declaration");
            if (t == "list") {
                pr.is_list = true;
                if (!(ls >> pr.count_type >> pr.type >> pr.name)) reject("invalid list declaration");
                if (!ply::scalar_type(pr.count_type).integral) reject("list count type is not integral");
            } else {
                pr.type = t;
                if (!(ls >> pr.name)) reject("invalid scalar declaration");
            }
            const auto type = ply::scalar_type(pr.type);
            if (pr.is_list && (pr.name == "vertex_indices" || pr.name == "vertex_index") &&
                !type.integral) reject("vertex index type is not integral");
            auto& props = cur == VERTEX ? vprops : fprops;
            if (props.size() >= 1024) reject("property count exceeds budget");
            props.push_back(std::move(pr));
        } else if (tok == "end_header") {
            header_end = static_cast<std::size_t>(is.tellg());
            have_end = true;
            break;
        }
    }
    if (!have_format || !have_vertex || !have_end) reject("incomplete header");
    int vx = -1, vy = -1, vz = -1;
    for (std::size_t i = 0; i < vprops.size(); ++i) {
        if (vprops[i].name == "x") vx = static_cast<int>(i);
        else if (vprops[i].name == "y") vy = static_cast<int>(i);
        else if (vprops[i].name == "z") vz = static_cast<int>(i);
    }
    if (vx < 0 || vy < 0 || vz < 0) reject("missing x/y/z vertex properties");
    if (vprops[vx].is_list || vprops[vy].is_list || vprops[vz].is_list)
        reject("coordinate properties must be scalars");
    std::size_t face_lists = 0;
    for (const auto& pr : fprops)
        if (pr.is_list && (pr.name == "vertex_indices" || pr.name == "vertex_index")) ++face_lists;
    if (n_face && face_lists != 1) reject("faces require one vertex index list");
    const std::size_t body_bytes = static_cast<std::size_t>(file_end) - header_end;
    if (n_vert > body_bytes / 3 || n_face > body_bytes)
        reject("element counts exceed available body bytes");

    MeshData mesh;
    mesh.verts.reserve(n_vert * 3);
    mesh.tris.reserve(n_face * 3);
    std::size_t total_list_items = 0;
    auto list_count = [&](double value) {
        if (!std::isfinite(value) || value < 0 || std::trunc(value) != value ||
            value > ply::kMaxListItems) reject("invalid or over-budget list count");
        const auto count = static_cast<std::size_t>(value);
        if (count > ply::kMaxTotalListItems - total_list_items) reject("total list budget exceeded");
        total_list_items += count;
        return count;
    };
    auto index_value = [&](double value) {
        if (!std::isfinite(value) || value < 0 || std::trunc(value) != value || value >= n_vert)
            reject("face vertex index is outside the vertex array");
        return static_cast<uint32_t>(value);
    };
    auto emit_face = [&](const std::vector<uint32_t>& indices) {
        const std::size_t triangles = indices.size() >= 3 ? indices.size() - 2 : 0;
        if (triangles > ply::kMaxElements - mesh.triangle_count())
            reject("triangulated face budget exceeded");
        for (std::size_t j = 1; j + 1 < indices.size(); ++j)
            mesh.tris.insert(mesh.tris.end(), {indices[0], indices[j], indices[j + 1]});
    };
    auto emit_vertex = [&](const std::array<double, 3>& coordinates) {
        for (double value : coordinates)
            if (!std::isfinite(value)) reject("non-finite vertex coordinate");
        mesh.verts.insert(mesh.verts.end(), coordinates.begin(), coordinates.end());
    };

    std::vector<unsigned char> buf;
    if (enc == BIN_LE) {
        buf.assign(std::istreambuf_iterator<char>(is), std::istreambuf_iterator<char>());
        buf.push_back(0);  // a non-null begin/end pair even for an empty payload
    }
    const unsigned char* p = buf.data();
    const unsigned char* end = enc == BIN_LE ? p + buf.size() - 1 : p;
    std::vector<uint32_t> indices;
    for (int element = 0; element < 2; ++element) {
        const auto& props = element == 0 ? vprops : fprops;
        const std::size_t count = element == 0 ? n_vert : n_face;
        for (std::size_t row = 0; row < count; ++row) {
            std::istringstream ls;
            if (enc == ASCII) {
                if (!std::getline(is, line)) reject("truncated ASCII element");
                ls.str(line);
            }
            auto scalar = [&](const std::string& type) {
                return enc == ASCII ? ply::read_ascii_scalar(ls, type) :
                                      ply::read_le_scalar(p, end, type);
            };
            std::array<double, 3> coordinates{};
            indices.clear();
            for (std::size_t k = 0; k < props.size(); ++k) {
                const auto& pr = props[k];
                if (pr.is_list) {
                    const std::size_t items = list_count(scalar(pr.count_type));
                    // Check the whole list before resizing or reading a single item.
                    if (enc == BIN_LE && items > static_cast<std::size_t>(end - p) /
                                                     ply::scalar_type(pr.type).width)
                        reject("truncated binary list payload");
                    if (enc == ASCII && items > line.size()) reject("truncated ASCII list payload");
                    const bool face_indices = element == 1 &&
                        (pr.name == "vertex_indices" || pr.name == "vertex_index");
                    if (face_indices) indices.reserve(items);
                    for (std::size_t j = 0; j < items; ++j) {
                        const double value = scalar(pr.type);
                        if (face_indices) indices.push_back(index_value(value));
                    }
                } else {
                    const double value = scalar(pr.type);
                    if (element == 0) {
                        if (static_cast<int>(k) == vx) coordinates[0] = value;
                        else if (static_cast<int>(k) == vy) coordinates[1] = value;
                        else if (static_cast<int>(k) == vz) coordinates[2] = value;
                    }
                }
            }
            if (element == 0) emit_vertex(coordinates);
            else emit_face(indices);
        }
    }
    return mesh;
}
