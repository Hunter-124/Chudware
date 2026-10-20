// Shared implementation for the flow importer and standalone sanitizer regressions.
// Included inside chudware::sim after the standard stream/string/vector/math headers.
// Load an STL (binary or ASCII, units = mm) into a triangle soup — see flow_run.h. No vertex
// dedup: the X-scanline voxelizer indexes per-triangle, so sharing vertices saves no work.
bool load_stl_soup(const std::string& path, std::vector<float>& pos,
                   std::vector<uint32_t>& idx, std::string& err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { err = "cannot open STL: " + path; return false; }
    std::string data((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (data.size() < 15) { err = "STL too small: " + path; return false; }

    // ASCII detection: starts with "solid" and carries a "facet" keyword (a binary header may
    // also start with the bytes "solid", so the facet check is what disambiguates).
    bool ascii = false;
    {
        size_t i = data.find_first_not_of(" \t\r\n");
        if (i != std::string::npos && data.compare(i, 5, "solid") == 0 &&
            data.find("facet", i) != std::string::npos)
            ascii = true;
    }

    if (ascii) {
        // Every "vertex x y z" line contributes one vertex; each consecutive triple one tri.
        const char* p = data.c_str();
        std::vector<float> v;
        while ((p = std::strstr(p, "vertex")) != nullptr) {
            p += 6;
            char* end = nullptr;
            float x = std::strtof(p, &end); if (end == p) { err = "bad ASCII STL vertex"; return false; } p = end;
            float y = std::strtof(p, &end); if (end == p) { err = "bad ASCII STL vertex"; return false; } p = end;
            float z = std::strtof(p, &end); if (end == p) { err = "bad ASCII STL vertex"; return false; } p = end;
            v.push_back(x); v.push_back(y); v.push_back(z);
        }
        if (v.size() < 9 || v.size() % 9 != 0) {
            err = "ASCII STL vertex count not a multiple of 3 per triangle";
            return false;
        }
        pos = std::move(v);
    } else {
        // Binary: 80-byte header, uint32 tri count, 50 bytes per tri (normal + 3 verts + attr).
        if (data.size() < 84) { err = "binary STL shorter than header"; return false; }
        uint32_t ntri = 0;
        std::memcpy(&ntri, data.data() + 80, 4);
        const uint64_t need = 84ull + 50ull * ntri;
        if (need != data.size()) { err = "binary STL size mismatch (not an STL?)"; return false; }
        pos.resize((size_t)ntri * 9);
        for (uint32_t t = 0; t < ntri; ++t) {
            const char* rec = data.data() + 84 + 50ull * t;
            std::memcpy(pos.data() + (size_t)t * 9, rec + 12, 9 * sizeof(float));
        }
    }
    if (pos.empty() ||
        !std::all_of(pos.begin(), pos.end(), [](float v) { return std::isfinite(v); })) {
        err = pos.empty() ? "STL contains no triangles" : "STL contains non-finite vertices";
        return false;
    }
    const size_t ntri = pos.size() / 9;
    idx.resize(ntri * 3);
    for (uint32_t i = 0; i < (uint32_t)(ntri * 3); ++i) idx[i] = i;
    return true;
}
