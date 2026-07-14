// .step+ container reader/writer — see stepplus.h.
//
// ZIP I/O via miniz (single-file; read + write). Mirrors engine/doc/stepplus.py:
//   * thumbnail.png written FIRST + STORED (uncompressed) so shell previewers read
//     it cheaply; a flat-grey 256x256 PNG placeholder (off-screen render is later).
//   * meta.json / document.json deflated.
//   * unknown entries preserved verbatim on resave; the entries we own are never
//     duplicated.
//   * atomic: write "<path>.tmp" then std::filesystem::rename (replace).

#include "doc/stepplus.h"

#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <set>
#include <sstream>
#include <string>
#include <vector>

#include <BinTools.hxx>
#include <BinTools_FormatVersion.hxx>

#include <miniz.h>

// OCCT version folded into the geometry-cache engine key when the kernel is present (slim
// non-OCCT builds compile without it). Preprocessor-guarded so it never breaks either build.
#if __has_include(<Standard_Version.hxx>)
#  include <Standard_Version.hxx>
#endif

#include "doc/session.h"  // OpError

#include "sim/sim_cache.h"

namespace aicad::doc {

using nlohmann::json;

namespace {

constexpr const char* kContainer = "aicad.step+/1";
constexpr const char* kAppVersion = "AI-CAD/M1";

// Entries this module owns + rewrites on every save; everything else is preserved.
bool is_known_entry(const std::string& name) {
    return name == "thumbnail.png" || name == "meta.json" ||
           name == "document.json" || name == "geometry.step";
}

// ------------------------------------------------------------------ thumbnail PNG
void put_u32_be(std::string& out, uint32_t v) {
    out.push_back(static_cast<char>((v >> 24) & 0xFF));
    out.push_back(static_cast<char>((v >> 16) & 0xFF));
    out.push_back(static_cast<char>((v >> 8) & 0xFF));
    out.push_back(static_cast<char>(v & 0xFF));
}

void png_chunk(std::string& out, const char tag[4], const std::string& data) {
    put_u32_be(out, static_cast<uint32_t>(data.size()));
    std::string body(tag, 4);
    body += data;
    out.append(body);
    mz_ulong crc = mz_crc32(MZ_CRC32_INIT,
                            reinterpret_cast<const unsigned char*>(body.data()),
                            body.size());
    put_u32_be(out, static_cast<uint32_t>(crc));
}

// A minimal valid flat-grey PNG (no external deps) — port of _placeholder_png.
std::string placeholder_png(int size = 256, unsigned char grey = 200) {
    std::string out;
    out.append("\x89PNG\r\n\x1a\n", 8);

    // IHDR: width, height, bitdepth=8, colortype=2 (RGB), compression/filter/interlace=0.
    std::string ihdr;
    put_u32_be(ihdr, static_cast<uint32_t>(size));
    put_u32_be(ihdr, static_cast<uint32_t>(size));
    ihdr.push_back(8);
    ihdr.push_back(2);
    ihdr.push_back(0);
    ihdr.push_back(0);
    ihdr.push_back(0);
    png_chunk(out, "IHDR", ihdr);

    // Raw scanlines: each row prefixed with filter byte 0, then size*RGB grey pixels.
    std::string raw;
    raw.reserve(static_cast<std::size_t>(size) * (1 + size * 3));
    std::string row(static_cast<std::size_t>(size) * 3, static_cast<char>(grey));
    for (int y = 0; y < size; ++y) {
        raw.push_back(0);
        raw.append(row);
    }
    // zlib-wrapped deflate (PNG IDAT) via miniz's zlib-compatible compressor.
    mz_ulong bound = mz_compressBound(static_cast<mz_ulong>(raw.size()));
    std::vector<unsigned char> comp(bound);
    mz_ulong comp_len = bound;
    if (mz_compress2(comp.data(), &comp_len,
                     reinterpret_cast<const unsigned char*>(raw.data()),
                     static_cast<mz_ulong>(raw.size()), 9) != MZ_OK) {
        // Should never happen for a fixed buffer; fall back to an empty IDAT.
        comp_len = 0;
    }
    png_chunk(out, "IDAT", std::string(reinterpret_cast<char*>(comp.data()), comp_len));
    png_chunk(out, "IEND", std::string());
    return out;
}

// ------------------------------------------------------------------ misc helpers
std::string iso_utc_now() {
    std::time_t t = std::time(nullptr);
    std::tm tmv{};
#if defined(_WIN32)
    gmtime_s(&tmv, &t);
#else
    gmtime_r(&t, &tmv);
#endif
    char buf[40];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                  tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
                  tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
    return std::string(buf);
}

[[noreturn]] void io_error(const std::string& msg) {
    throw OpError("io_error", msg, json{{"reason", msg}});
}

// ------------------------------------------------------------------ canonical JSON
//
// Policy (T-H2 / contracts/doc_schema.md §Canonical dump):
//   - Floats are rounded at serialize time ONLY (in-memory precision unchanged):
//       * mm-valued quantities: 6 decimal places (µm resolution)
//       * degree-valued quantities: 3 decimal places (1e-3 deg resolution)
//   - Both length and angle fields are stored as the same float in JSON, so we
//     apply a single rounding pass at 6 dp (µm covers both: 1e-3 deg ≫ needed).
//   - nlohmann::json uses std::map → keys are already sorted alphabetically.
//   - The resulting string is deterministic for the same document tree.
//
// Recursively round every JSON float to `decimals` decimal places.
json round_floats(const json& v, int decimals = 6) {
    const double factor = std::pow(10.0, decimals);
    if (v.is_number_float()) {
        double d = v.get<double>();
        return json(std::round(d * factor) / factor);
    }
    if (v.is_array()) {
        json out = json::array();
        for (const auto& elem : v) out.push_back(round_floats(elem, decimals));
        return out;
    }
    if (v.is_object()) {
        json out = json::object();
        for (auto& [k, val] : v.items()) out[k] = round_floats(val, decimals);
        return out;
    }
    return v;  // bool, int, null, string — unchanged
}

// Return the canonical document.json serialization:
//   round_floats(doc, 6) then dump(2).  Keys are sorted because nlohmann::json
//   (std::map) always iterates in lexicographic order.
std::string canonical_doc_json(const json& doc) {
    return round_floats(doc).dump(2);
}

}  // namespace

// ------------------------------------------------------------------------- save
std::string stepplus_save(const json& doc, const std::string& path,
                          const std::map<std::string, std::string>& extra) {
    namespace fs = std::filesystem;
    fs::path out_path(path);
    fs::path tmp_path = out_path;
    tmp_path += ".tmp";

    const std::string now = iso_utc_now();
    const std::string schema = doc.value("schema", std::string());
    const std::string kind = (schema == "aicad.asm/1") ? "assembly" : "part";

    json meta = {
        {"container", kContainer},
        {"app_version", kAppVersion},
        {"kind", kind},
        {"created", now},
        {"modified", now},
        {"units", doc.contains("units") ? doc["units"] : json{{"system", "MMGS"}}},
        {"name", doc.value("name", std::string("Untitled"))},
    };

    // Canonical dump: sorted keys (std::map) + float rounding (6 dp = µm grade).
    const std::string document = canonical_doc_json(doc);
    const std::string meta_json = meta.dump(2);  // meta.json timestamps exempt from stability DoD

    // thumbnail: reuse a carried one if present, else the flat-grey placeholder.
    std::string thumb;
    auto th = extra.find("thumbnail.png");
    thumb = (th != extra.end()) ? th->second : placeholder_png();

    if (out_path.has_parent_path()) {
        std::error_code ec;
        fs::create_directories(out_path.parent_path(), ec);
    }

    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_writer_init_file(&zip, tmp_path.string().c_str(), 0))
        io_error("doc_save: cannot create archive: " + tmp_path.string());

    auto add = [&](const std::string& name, const std::string& data, bool stored) {
        mz_uint level = stored ? (mz_uint)MZ_NO_COMPRESSION : (mz_uint)MZ_DEFAULT_LEVEL;
        if (!mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(), level)) {
            mz_zip_writer_end(&zip);
            std::error_code ec;
            std::filesystem::remove(tmp_path, ec);
            io_error("doc_save: cannot add entry '" + name + "'");
        }
    };

    // thumbnail FIRST + STORED, then the deflated JSON entries.
    add("thumbnail.png", thumb, /*stored=*/true);
    add("meta.json", meta_json, /*stored=*/false);
    add("document.json", document, /*stored=*/false);
    // Preserve unknown entries (forward-compat) — never clobber the entries we own.
    // cache.aicm (the persisted-geometry mesh blob) is written STORED, not deflated: it is
    // large binary that barely compresses, and STORED lets doc_open extract it raw with
    // MZ_ZIP_FLAG_COMPRESSED_DATA — zero inflate AND zero CRC32 — which together dominated
    // open self-time (measured mz_crc32 47% + tinfl_decompress 34% of doc_open). Any zip
    // reader still reads a STORED entry transparently, so this stays fully back/forward
    // compatible; the cache is display-only + hash-gated, so the skipped zip CRC is fine.
    for (const auto& [name, data] : extra) {
        if (is_known_entry(name)) continue;
        // cache.aicm + studies/*/results.npz are large binary blobs — STORED (no deflate).
        const bool stored = (name == "cache.aicm") ||
                            (name.rfind("studies/", 0) == 0 && name.find("/results.") != std::string::npos);
        add(name, data, /*stored=*/stored);
    }

    if (!mz_zip_writer_finalize_archive(&zip)) {
        mz_zip_writer_end(&zip);
        std::error_code ec;
        fs::remove(tmp_path, ec);
        io_error("doc_save: cannot finalize archive");
    }
    mz_zip_writer_end(&zip);

    std::error_code ec;
    fs::rename(tmp_path, out_path, ec);  // atomic replace (MoveFileEx on Windows)
    if (ec) {
        fs::remove(tmp_path, ec);
        io_error("doc_save: cannot replace '" + out_path.string() + "'");
    }
    return out_path.string();
}

// ------------------------------------------------------------------------- open
StepPlusDoc stepplus_open(const std::string& path) {
    if (!std::filesystem::exists(path)) io_error("doc_open: no such file: " + path);

    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_file(&zip, path.c_str(), 0))
        io_error("doc_open: not a valid .step+ (zip) container: " + path);

    auto read_entry = [&](const std::string& name, std::string& out) -> bool {
        int idx = mz_zip_reader_locate_file(&zip, name.c_str(), nullptr, 0);
        if (idx < 0) return false;
        std::size_t n = 0;
        void* p = mz_zip_reader_extract_to_heap(&zip, (mz_uint)idx, &n, 0);
        if (!p) return false;
        out.assign(static_cast<char*>(p), n);
        mz_free(p);
        return true;
    };

    StepPlusDoc result;

    std::string meta_bytes;
    if (!read_entry("meta.json", meta_bytes)) {
        mz_zip_reader_end(&zip);
        io_error("not a .step+ container: meta.json missing");
    }
    json meta;
    try {
        meta = json::parse(meta_bytes);
    } catch (const std::exception& e) {
        mz_zip_reader_end(&zip);
        io_error(std::string("doc_open: bad meta.json: ") + e.what());
    }
    const std::string container = meta.value("container", std::string());
    if (container.rfind("aicad.step+/", 0) != 0) {
        mz_zip_reader_end(&zip);
        io_error("unknown container '" + container + "'");
    }
    const std::string major =
        (container.find('/') != std::string::npos) ? container.substr(container.rfind('/') + 1) : "";
    if (!major.empty() && major != "1") {
        mz_zip_reader_end(&zip);
        io_error("unsupported .step+ major version '" + major + "'");
    }

    std::string doc_bytes;
    if (!read_entry("document.json", doc_bytes)) {
        mz_zip_reader_end(&zip);
        io_error("not a .step+ container: document.json missing");
    }
    try {
        result.doc = json::parse(doc_bytes);
    } catch (const std::exception& e) {
        mz_zip_reader_end(&zip);
        io_error(std::string("doc_open: bad document.json: ") + e.what());
    }

    // Carry every entry we don't own back onto the session for forward-compat resave.
    mz_uint count = mz_zip_reader_get_num_files(&zip);
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) continue;
        const std::string name = st.m_filename;
        if (is_known_entry(name)) continue;
        // STORED entries (method 0 — e.g. cache.aicm) carry the persisted-geometry mesh. Extract
        // them raw with MZ_ZIP_FLAG_COMPRESSED_DATA so miniz skips BOTH the DEFLATE inflate and
        // the byte-wise mz_crc32 verify (for a STORED entry the "compressed" bytes ARE the data).
        // This is the doc_open / agent->GUI handoff fast path — it was the dominant open cost.
        // DEFLATE entries take the normal extract (they must be inflated).
        std::size_t n = 0;
        mz_uint flags = (st.m_method == 0) ? MZ_ZIP_FLAG_COMPRESSED_DATA : 0;
        void* p = mz_zip_reader_extract_to_heap(&zip, i, &n, flags);
        if (!p) continue;
        result.extra[name].assign(static_cast<char*>(p), n);
        mz_free(p);
    }

    mz_zip_reader_end(&zip);
    return result;
}

// --- persisted-geometry cache keys ------------------------------------------------
namespace {
// FNV-1a 64-bit — deterministic across runs/platforms (unlike std::hash), which matters
// because this value is written into the file and compared on a later open.
std::string fnv1a_hex(const std::string& s) {
    uint64_t h = 1469598103934665603ULL;
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ULL;
    }
    char buf[17];
    std::snprintf(buf, sizeof(buf), "%016llx", (unsigned long long)h);
    return std::string(buf);
}
}  // namespace

std::string stepplus_doc_hash(const json& doc) {
    // Hash the SAME canonical form stepplus_save writes, so an unchanged saved tree hashes
    // identically on reopen (a hand-edit of document.json changes it -> cache rejected). Strip
    // the volatile top-level "id" first: doc_open re-stamps it to the new session id BEFORE the
    // hash check, and it never affects geometry — leaving it in would miss every cache.
    if (doc.is_object() && doc.contains("id")) {
        json d = doc;
        d.erase("id");
        return fnv1a_hex(canonical_doc_json(d));
    }
    return fnv1a_hex(canonical_doc_json(doc));
}

std::string brep_cache_encode(const std::vector<TopoDS_Shape>& bodies) {
    std::string out = "AICBREP1";
    auto put_u64 = [&out](uint64_t v) {
        for (int i = 0; i < 8; ++i) out.push_back(static_cast<char>((v >> (8 * i)) & 0xff));
    };
    put_u64(bodies.size());
    for (const TopoDS_Shape& s : bodies) {
        std::ostringstream os(std::ios::binary);
        // WITHOUT triangulation: BinTools-restored triangulation sends BRepMesh into a
        // runaway Delaunay refinement on NURBS faces (measured 5.3GB RSS on a lofted wing
        // vs 200MB for the same shape without it — OCCT 7.9). The shapes still skip
        // compile_document; meshing happens once per session via the assembly tess cache,
        // and unchanged-assembly reopens replay cache.aicm without meshing at all.
        BinTools::Write(s, os, /*theWithTriangles=*/Standard_False,
                        /*theWithNormals=*/Standard_False, BinTools_FormatVersion_VERSION_3);
        std::string bytes = os.str();
        put_u64(bytes.size());
        out += bytes;
    }
    return out;
}

std::vector<TopoDS_Shape> brep_cache_decode(const std::string& blob) {
    auto bad = [](const std::string& why) -> OpError {
        return OpError("io_error", "cache.brep: " + why, json{{"reason", why}});
    };
    std::size_t pos = 0;
    auto get_u64 = [&](const char* what) -> uint64_t {
        if (pos + 8 > blob.size()) throw bad(std::string("truncated ") + what);
        uint64_t v = 0;
        for (int i = 0; i < 8; ++i)
            v |= static_cast<uint64_t>(static_cast<unsigned char>(blob[pos + i])) << (8 * i);
        pos += 8;
        return v;
    };
    if (blob.size() < 8 || blob.compare(0, 8, "AICBREP1") != 0) throw bad("bad magic");
    pos = 8;
    const uint64_t count = get_u64("count");
    if (count > 100000) throw bad("implausible body count");
    std::vector<TopoDS_Shape> bodies;
    bodies.reserve(static_cast<std::size_t>(count));
    for (uint64_t i = 0; i < count; ++i) {
        const uint64_t len = get_u64("length");
        if (pos + len > blob.size()) throw bad("truncated body");
        std::istringstream is(blob.substr(pos, static_cast<std::size_t>(len)), std::ios::binary);
        pos += static_cast<std::size_t>(len);
        TopoDS_Shape sh;
        BinTools::Read(sh, is);
        if (sh.IsNull()) throw bad("null shape");
        bodies.push_back(sh);
    }
    return bodies;
}

std::string stepplus_cache_engine_key() {
    // Bump CACHE_SCHEMA on ANY geometry-compiler change that could alter output geometry, so
    // caches written by an older engine are rejected. OCCT version folded in when available.
    static constexpr const char* kCacheSchema = "aicad-geomcache/1";
#ifdef OCC_VERSION_COMPLETE
    return std::string(kCacheSchema) + ";occt=" OCC_VERSION_COMPLETE;
#else
    return std::string(kCacheSchema) + ";occt=none";
#endif
}

// --- studies/<id>/results.npz -------------------------------------------------
std::string stepplus_study_result_entry(const std::string& study_id) {
    return aicad::sim::cache::study_result_entry(study_id);
}

void stepplus_erase_study_results(std::map<std::string, std::string>& extra) {
    aicad::sim::cache::erase_study_results(extra);
}

bool stepplus_put_study_result(std::map<std::string, std::string>& extra,
                               const std::string& study_id, const std::string& kind,
                               const std::string& input_hash, const json& result) {
    using aicad::sim::cache::Kind;
    Kind k = Kind::FeaStatic;
    if (kind == "fea_static") k = Kind::FeaStatic;
    else if (kind == "fea_modal") k = Kind::FeaModal;
    else if (kind == "fea_thermal") k = Kind::FeaThermal;
    else if (kind == "spice") k = Kind::Spice;
    else if (kind == "cfd") k = Kind::Cfd;
    else if (kind == "em") k = Kind::Em;
    else return false;
    return aicad::sim::cache::put_study_result(extra, study_id, k, input_hash, result);
}

bool stepplus_get_study_result(const std::map<std::string, std::string>& extra,
                               const std::string& study_id, std::string& kind_out,
                               std::string& input_hash_out, json& result_out) {
    aicad::sim::cache::Kind k;
    if (!aicad::sim::cache::get_study_result(extra, study_id, k, input_hash_out, result_out))
        return false;
    kind_out = aicad::sim::cache::kind_name(k);
    return true;
}

}  // namespace aicad::doc
