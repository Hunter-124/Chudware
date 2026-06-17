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
#include <cstdint>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <set>
#include <string>
#include <vector>

#include <miniz.h>

#include "doc/session.h"  // OpError

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

    const std::string document = doc.dump(2);
    const std::string meta_json = meta.dump(2);

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
    for (const auto& [name, data] : extra) {
        if (is_known_entry(name)) continue;
        add(name, data, /*stored=*/false);
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
        std::string data;
        if (read_entry(name, data)) result.extra[name] = std::move(data);
    }

    mz_zip_reader_end(&zip);
    return result;
}

}  // namespace aicad::doc
