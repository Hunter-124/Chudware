// .step+ container reader/writer — see stepplus.h.
//
// ZIP I/O via miniz (single-file; read + write). Mirrors engine/doc/stepplus.py:
//   * thumbnail.png written FIRST + STORED (uncompressed) so shell previewers read
//     it cheaply; a flat-grey 256x256 PNG placeholder (off-screen render is later).
//   * meta.json / document.json deflated.
//   * unknown entries preserved verbatim on resave; the entries we own are never
//     duplicated.
//   * atomic: write an owner-private, exclusively created stage, then publish it.

#if defined(_WIN32)
#define _CRT_RAND_S
#endif
#include "doc/stepplus.h"

#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <set>
#include <sstream>
#include <string>
#include <vector>
#include <fstream>
#include <mutex>
#include "util/sha256.h"

#if defined(_WIN32)
#  ifndef NOMINMAX
#    define NOMINMAX
#  endif
#  include <windows.h>
#  include <io.h>
#  include <fcntl.h>
#  include "../../services/ipc/ipc_private.h"
#else
#  include <cerrno>
#  include <fcntl.h>
#  include <sys/stat.h>
#  include <unistd.h>
#endif

#include <BinTools.hxx>
#include <BinTools_FormatVersion.hxx>
#include <DESTEP_Parameters.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <STEPControl_StepModelType.hxx>
#include <STEPControl_Writer.hxx>
#include <TopAbs.hxx>
#include <TopExp_Explorer.hxx>
#include <UnitsMethods_LengthUnit.hxx>

#include <miniz.h>

// OCCT version folded into the geometry-cache engine key when the kernel is present (slim
// non-OCCT builds compile without it). Preprocessor-guarded so it never breaks either build.
#if __has_include(<Standard_Version.hxx>)
#  include <Standard_Version.hxx>
#endif

#include "doc/session.h"  // OpError

#include "sim/sim_cache.h"
#include "util/time_local.h"

namespace chudware::doc {

using nlohmann::json;

namespace {

constexpr const char* kContainer = "aicad.step+/1";
constexpr const char* kAppVersion = "Chudware/M1";
// Conservative pre-allocation limits for remotely supplied archives. Metadata is checked
// for every entry before any payload buffer is allocated.
constexpr mz_uint kMaxArchiveEntries = 4096;
constexpr mz_uint64 kMaxEntryExpandedBytes = 64ull * 1024ull * 1024ull;
constexpr mz_uint64 kMaxArchiveExpandedBytes = 256ull * 1024ull * 1024ull;
constexpr mz_uint64 kMaxCompressionRatio = 200;

[[noreturn]] void archive_limit_error(const char* limit, const std::string& format_name) {
    throw OpError("io_error", "doc_open: " + format_name + " archive exceeds " + limit,
                  {{"limit", limit}});
}

// Entries every editable container owns + rewrites. geometry.step is owned only
// by .step+; in .draw+ it is an unknown forward-compatible entry and survives.
bool is_known_entry(const std::string& name, bool owns_geometry_step) {
    return name == "thumbnail.png" || name == "meta.json" ||
           name == "document.json" ||
           (owns_geometry_step && name == "geometry.step");
}

bool is_geometry_cache(const std::string& name) {
    return name == "cache.json" || name == "cache.aicm" || name == "cache.brep" ||
           name == "cache.ckpt";
}

std::string provenance_key() {
    namespace fs = std::filesystem;
    static std::mutex mutex;
    std::lock_guard<std::mutex> lock(mutex);
    fs::path state = fs::u8path(native_policy::setting(true));
    if (state.empty()) {
#if defined(_WIN32)
        const wchar_t* xdg = _wgetenv(L"XDG_DATA_HOME");
        const wchar_t* home = _wgetenv(L"LOCALAPPDATA");
#else
        const char* xdg = std::getenv("XDG_DATA_HOME");
        const char* home = std::getenv("HOME");
#endif
        if (xdg && *xdg) state = fs::path(xdg) / "Chudware";
        else if (home && *home) state = fs::path(home) /
#if defined(_WIN32)
            "Chudware"
#else
            ".local/share/Chudware"
#endif
        ;
        else throw std::runtime_error("cache provenance state directory unavailable");
    }
    fs::create_directories(state);
    const fs::path path = state / "cache-provenance.key";
    std::string key(32, '\0');
#if defined(_WIN32)
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                              OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (file == INVALID_HANDLE_VALUE && GetLastError() == ERROR_FILE_NOT_FOUND) {
        for (size_t i = 0; i < key.size(); i += sizeof(unsigned int)) {
            unsigned int random = 0;
            if (rand_s(&random) != 0) throw std::runtime_error("cache key entropy unavailable");
            std::memcpy(key.data() + i, &random, sizeof(random));
        }
        auto& security = chudware_ipc::privateSecurity();
        if (!security.valid()) throw std::runtime_error("cache key ACL unavailable");
        const fs::path temporary = path.native() + L".new-" +
            std::to_wstring(GetCurrentProcessId()) + L"-" + std::to_wstring(GetTickCount64());
        file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, &security.attributes,
                           CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) throw std::runtime_error("cache key staging failed");
        DWORD written = 0;
        bool ok = WriteFile(file, key.data(), 32, &written, nullptr) && written == 32 &&
                  FlushFileBuffers(file);
        CloseHandle(file);
        if (ok && !MoveFileW(temporary.c_str(), path.c_str())) {
            const DWORD error = GetLastError();
            ok = error == ERROR_ALREADY_EXISTS || error == ERROR_FILE_EXISTS;
        }
        DeleteFileW(temporary.c_str());
        if (!ok) throw std::runtime_error("cache key publication failed");
        file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                           OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    }
    if (file == INVALID_HANDLE_VALUE) throw std::runtime_error("cache key open failed");
    BY_HANDLE_FILE_INFORMATION info{};
    DWORD bytes = 0;
    const bool ok = GetFileInformationByHandle(file, &info) &&
                   !(info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) &&
                   info.nFileSizeHigh == 0 && info.nFileSizeLow == 32 &&
                   ReadFile(file, key.data(), 32, &bytes, nullptr) && bytes == 32;
    CloseHandle(file);
    if (!ok) throw std::runtime_error("cache key invalid");
#else
    int file = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (file < 0 && errno == ENOENT) {
        const int random = ::open("/dev/urandom", O_RDONLY | O_CLOEXEC);
        if (random < 0) throw std::runtime_error("cache key entropy unavailable");
        size_t offset = 0;
        while (offset < key.size()) {
            const ssize_t n = ::read(random, key.data() + offset, key.size() - offset);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { ::close(random); throw std::runtime_error("cache key entropy failed"); }
            offset += static_cast<size_t>(n);
        }
        ::close(random);
        const std::string temporary = path.string() + ".new-" + std::to_string(::getpid());
        const int stage = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW |
                                                  O_CLOEXEC, 0600);
        if (stage < 0) throw std::runtime_error("cache key staging failed");
        offset = 0;
        bool ok = true;
        while (offset < key.size()) {
            const ssize_t n = ::write(stage, key.data() + offset, key.size() - offset);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) { ok = false; break; }
            offset += static_cast<size_t>(n);
        }
        if (::fsync(stage) != 0) ok = false;
        ::close(stage);
        if (ok && ::link(temporary.c_str(), path.c_str()) != 0 && errno != EEXIST) ok = false;
        ::unlink(temporary.c_str());
        if (!ok) throw std::runtime_error("cache key publication failed");
        file = ::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    }
    if (file < 0) throw std::runtime_error("cache key open failed");
    struct stat info{};
    const bool valid = ::fstat(file, &info) == 0 && S_ISREG(info.st_mode) &&
                       info.st_uid == ::geteuid() && (info.st_mode & 0777) == 0600 &&
                       info.st_size == 32;
    const ssize_t bytes = valid ? ::read(file, key.data(), key.size()) : -1;
    ::close(file);
    if (bytes != 32) throw std::runtime_error("cache key invalid");
#endif
    return key;
}

std::string provenance_mac(const json& manifest) {
    const std::string key = provenance_key();
    std::array<uint8_t, 64> inner{}, outer{};
    for (size_t i = 0; i < inner.size(); ++i) {
        const uint8_t byte = i < key.size() ? static_cast<uint8_t>(key[i]) : 0;
        inner[i] = byte ^ 0x36; outer[i] = byte ^ 0x5c;
    }
    const std::string bytes = manifest.dump();
    util::Sha256 ih;
    ih.update(inner.data(), inner.size());
    ih.update(reinterpret_cast<const uint8_t*>(bytes.data()), bytes.size());
    const auto digest = ih.finish();
    util::Sha256 oh;
    oh.update(outer.data(), outer.size()); oh.update(digest.data(), digest.size());
    return oh.finish_hex();
}

json cache_manifest(const std::string& doc_hash) {
    return {{"engine", stepplus_cache_engine_key()}, {"doc_hash", doc_hash},
            {"members", json::object()}};
}

std::string sign_manifest(const json& manifest) {
    return json{{"schema", "chudware-cache-provenance/1"}, {"manifest", manifest},
                {"mac", provenance_mac(manifest)}}.dump();
}

// Caches the writer admits only into the archive budget left after every essential and
// forward-compatible payload (mesh first, then build checkpoints, in name order). Both
// are large binary blobs written STORED.
bool is_optional_cache(const std::string& name) {
    return name == "cache.aicm" || name == "cache.ckpt";
}

// Keep the writer file open through publication; paths never reopen a reserved
// staging file. Cleanup removes only our single file, never an unrelated tree.
struct NativeStage {
    std::filesystem::path directory;
    std::FILE* stream = nullptr;
#if defined(_WIN32)
    HANDLE handle = INVALID_HANDLE_VALUE;
    int descriptor = -1;
    bool published = false;
#else
    int directory_fd = -1;
#endif
    ~NativeStage() {
#if defined(_WIN32)
        if (handle != INVALID_HANDLE_VALUE && !published) {
            FILE_DISPOSITION_INFO disposition{TRUE};
            SetFileInformationByHandle(handle, FileDispositionInfo,
                                      &disposition, sizeof(disposition));
        }
        if (stream) std::fclose(stream);
        else if (descriptor >= 0) _close(descriptor);
        else if (handle != INVALID_HANDLE_VALUE) CloseHandle(handle);
#else
        if (stream) std::fclose(stream);
        if (directory_fd >= 0) {
            ::unlinkat(directory_fd, "container.tmp", 0);
            ::close(directory_fd);
        }
#endif
        if (!directory.empty()) {
            std::error_code ignored;
            std::filesystem::remove(directory, ignored);
        }
    }
};

// miniz's CFILE reader never owns its FILE*. Close both layers before replacing
// a source archive: a CRT read handle otherwise denies deletion on Windows.
struct NativeArchiveReader {
    mz_zip_archive zip{};
    std::FILE* stream = nullptr;

    bool open(const std::filesystem::path& path) {
#if defined(_WIN32)
        stream = _wfopen(path.c_str(), L"rb");
#else
        stream = std::fopen(path.c_str(), "rb");
#endif
        return stream && mz_zip_reader_init_cfile(&zip, stream, 0, 0);
    }

    void close() {
        if (zip.m_pState) mz_zip_reader_end(&zip);
        if (stream) {
            std::fclose(stream);
            stream = nullptr;
        }
    }

    ~NativeArchiveReader() { close(); }
};

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
    (void)chudware::util::gmTime(t, tmv);
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
// Policy (contracts/doc_schema.md §Canonical dump):
//   - JSON numbers retain their full round-trip precision. Rounding dimensionless
//     axes or rigid transforms changes the constraints when a document is reopened.
//   - nlohmann::json uses std::map → keys are already sorted alphabetically.
//   - The resulting string is deterministic for the same document tree.
//
// Return the canonical document.json serialization without copying the tree unless
// an optional root entry must be omitted (notably an oversized simulation cache).
// nlohmann::json (std::map) emits keys in lexicographic order.
std::string canonical_doc_json(const json& doc,
                               const char* skip_root_key = nullptr) {
    if (skip_root_key == nullptr || !doc.is_object() || !doc.contains(skip_root_key))
        return doc.dump(2);
    json filtered = json::object();
    for (const auto& [key, value] : doc.items())
        if (key != skip_root_key) filtered[key] = value;
    return filtered.dump(2);
}

// meta.json for a fresh write (timestamps are fixed-width, so its size does not depend
// on the moment it is built — the budget estimate below relies on that).
json container_meta(const json& doc, const std::string& container, const std::string& kind) {
    const std::string now = iso_utc_now();
    return {
        {"container", container},
        {"app_version", kAppVersion},
        {"kind", kind},
        {"created", now},
        {"modified", now},
        {"units", doc.contains("units") ? doc["units"] : json{{"system", "MMGS"}}},
        {"name", doc.value("name", std::string("Untitled"))},
    };
}

// Expanded archive bytes left for the optional caches once document/meta/thumbnail,
// geometry.step and every other preserved entry are reserved (the reader admits caches
// only into this same remainder, so a cache that does not fit here is never written).
mz_uint64 optional_cache_budget(std::size_t document_bytes, std::size_t meta_bytes,
                                std::size_t thumb_bytes,
                                const std::map<std::string, std::string>& extra,
                                bool owns_geometry_step) {
    mz_uint64 budget = kMaxArchiveExpandedBytes;
    auto reserve_payload = [&](std::size_t bytes) {
        budget = bytes > budget ? 0 : budget - bytes;
    };
    if (owns_geometry_step) reserve_payload(2048); // bounded provenance envelope
    reserve_payload(document_bytes);
    reserve_payload(meta_bytes);
    reserve_payload(thumb_bytes);
    for (const auto& [name, data] : extra) {
        if ((owns_geometry_step && name == "geometry.step") ||
            (!is_known_entry(name, owns_geometry_step) &&
             !(owns_geometry_step && is_optional_cache(name))))
            reserve_payload(data.size());
    }
    return budget;
}

}  // namespace
std::string editable_document_json(const json& doc) {
    return canonical_doc_json(doc);
}

void configure_native_policy(const std::string& root, const std::string& state_dir) {
    native_policy::configure(root, state_dir);
}

bool stepplus_cache_verified(const json& doc,
                            const std::map<std::string, std::string>& entries,
                            bool component_only) {
    try {
        auto provenance = entries.find("cache.provenance");
        if (provenance == entries.end()) return false;
        const json envelope = json::parse(provenance->second);
        if (envelope.value("schema", std::string()) != "chudware-cache-provenance/1") return false;
        const json expected_identity = cache_manifest(util::sha256_hex(editable_document_json(doc)));
        const auto& manifest = envelope.at("manifest");
        if (manifest.at("engine") != expected_identity["engine"] ||
            manifest.at("doc_hash") != expected_identity["doc_hash"] ||
            !manifest.at("members").is_object()) return false;
        // Bounded readers can omit unrelated members. Authenticate the complete signed
        // manifest, then check each admitted/consumed payload independently.
        size_t checked = 0;
        for (const auto& [name, bytes] : entries) {
            if (!is_geometry_cache(name)) continue;
            if (component_only && name != "cache.json" && name != "cache.brep") continue;
            if (manifest.at("members").at(name) != util::sha256_hex(bytes)) return false;
            ++checked;
        }
        if (checked == 0 || (component_only &&
            (!entries.count("cache.json") || !entries.count("cache.brep")))) return false;
        const std::string actual = envelope.at("mac").get<std::string>();
        const std::string expected = provenance_mac(manifest);
        if (actual.size() != expected.size()) return false;
        unsigned char diff = 0;
        for (size_t i = 0; i < actual.size(); ++i) diff |= actual[i] ^ expected[i];
        return diff == 0;
    } catch (...) { return false; }
}

// ---------------------------------------------------------------- shared editable-container save
static std::string editable_container_save_impl(const json& doc, const std::string& path,
                                    const std::map<std::string, std::string>& extra,
                                    const std::string& container, const std::string& kind,
                                    const std::string& format_name,
                                    const json* preserved_meta = nullptr,
                                    bool exclusive = false,
                                    std::vector<std::string>* dropped = nullptr) {
    const bool owns_geometry_step = container.rfind("aicad.step+/", 0) == 0;
    namespace fs = std::filesystem;
    fs::path out_path = fs::u8path(path);
    fs::path tmp_path;
    NativeStage stage;
    const fs::path parent = out_path.has_parent_path() ? out_path.parent_path() : fs::path(".");
    std::error_code stage_error;
    fs::create_directories(parent, stage_error);
    if (stage_error) io_error("doc_save: cannot create destination parent: " + stage_error.message());
    static std::atomic<uint64_t> serial{0};
#if defined(_WIN32)
    auto& security = chudware_ipc::privateSecurity();
    if (!security.valid()) io_error("doc_save: cannot create owner-private staging policy");
#endif
    for (int attempt = 0; attempt < 100; ++attempt) {
        const auto candidate = parent /
            (".native-save-" + std::to_string(
                std::chrono::steady_clock::now().time_since_epoch().count()) + "-" +
             std::to_string(serial.fetch_add(1)));
#if defined(_WIN32)
        if (CreateDirectoryW(candidate.c_str(), &security.attributes)) {
            stage.directory = candidate;
            break;
        }
        if (GetLastError() != ERROR_ALREADY_EXISTS)
            io_error("doc_save: cannot reserve staging directory");
#else
        if (::mkdir(candidate.c_str(), 0700) == 0) {
            stage.directory = candidate;
            break;
        }
        if (errno != EEXIST) io_error("doc_save: cannot reserve staging directory");
#endif
    }
    if (stage.directory.empty()) io_error("doc_save: cannot reserve staging directory");
    tmp_path = stage.directory / "container.tmp";
#if defined(_WIN32)
    HANDLE handle = CreateFileW(tmp_path.c_str(), GENERIC_WRITE | DELETE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ, &security.attributes, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY |
        FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (handle == INVALID_HANDLE_VALUE) io_error("doc_save: cannot exclusively create staging file");
    stage.handle = handle;
    stage.descriptor = _open_osfhandle(reinterpret_cast<intptr_t>(handle), _O_BINARY | _O_WRONLY);
    if (stage.descriptor < 0) io_error("doc_save: cannot open staging stream");
    stage.stream = _fdopen(stage.descriptor, "wb");
    if (!stage.stream) io_error("doc_save: cannot open staging stream");
#else
    stage.directory_fd = ::open(stage.directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    struct stat directory_info{};
    if (stage.directory_fd < 0 || ::fstat(stage.directory_fd, &directory_info) != 0 ||
        directory_info.st_uid != ::geteuid() || (directory_info.st_mode & 0077) != 0)
        io_error("doc_save: staging directory identity changed");
    const int fd = ::openat(stage.directory_fd, "container.tmp",
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) io_error("doc_save: cannot exclusively create staging file");
    stage.stream = ::fdopen(fd, "wb");
    if (!stage.stream) {
        ::close(fd);
        io_error("doc_save: cannot open staging stream");
    }
#endif

    json meta = container_meta(doc, container, kind);
    if (preserved_meta) {
        meta = *preserved_meta;
        if (doc.contains("name")) meta["name"] = doc["name"];
    }

    // Canonical dump: sorted keys and lossless numeric serialization.
    const std::string document = editable_document_json(doc);
    const std::string meta_json = meta.dump(2);  // meta.json timestamps exempt from stability DoD

    // thumbnail: reuse a carried one if present, else the flat-grey placeholder.
    std::string thumb;
    auto th = extra.find("thumbnail.png");
    thumb = (th != extra.end()) ? th->second : (preserved_meta ? std::string() : placeholder_png());

    // Essential/forward-compatible payloads have priority over the optional caches
    // (cache.aicm, cache.ckpt). Reserve their expanded bytes before deciding whether each
    // STORED optional blob fits.
    mz_uint64 cache_budget = optional_cache_budget(document.size(), meta_json.size(),
                                                   thumb.size(), extra, owns_geometry_step);

    if (out_path.has_parent_path()) {
        std::error_code ec;
        fs::create_directories(out_path.parent_path(), ec);
    }

    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_writer_init_cfile(&zip, stage.stream, 0)) {
        if (format_name == ".step+")
            io_error("doc_save: cannot create archive: " + native_policy::path_utf8(tmp_path));
        io_error("doc_save: cannot create " + format_name + " archive: " +
                 native_policy::path_utf8(tmp_path));
    }

    auto add = [&](const std::string& name, const std::string& data, bool stored) {
        mz_uint level = stored ? (mz_uint)MZ_NO_COMPRESSION : (mz_uint)MZ_DEFAULT_LEVEL;
        if (!mz_zip_writer_add_mem(&zip, name.c_str(), data.data(), data.size(), level)) {
            mz_zip_writer_end(&zip);
            io_error("doc_save: cannot add entry '" + name + "'");
        }
    };

    // thumbnail FIRST + STORED, then the deflated JSON entries.
    if (!preserved_meta || th != extra.end()) add("thumbnail.png", thumb, /*stored=*/true);
    // Relocation uses STORED entries: a previously STORED, highly compressible
    // payload must not become an inadmissible compression-ratio archive on resave.
    add("meta.json", meta_json, /*stored=*/preserved_meta != nullptr);
    add("document.json", document, /*stored=*/preserved_meta != nullptr);
    // geometry.step: the contracted AP214 snapshot of the last successful rebuild
    // (contracts/stepplus.md §1). doc_save puts the bytes in `extra` after a fully-ok
    // rebuild; we write it as an entry we OWN so it lands right after document.json and
    // is never duplicated by the preserve loop below (is_known_entry skips it). Absent
    // bytes mean "this document has never rebuilt successfully" — the only omission the
    // contract allows. A .draw+ container does not own it, so there it stays an unknown
    // forward-compatible entry and survives through the loop verbatim.
    if (owns_geometry_step) {
        const auto geom = extra.find("geometry.step");
        if (geom != extra.end() && (!geom->second.empty() || preserved_meta))
            add("geometry.step", geom->second, /*stored=*/preserved_meta != nullptr);
    }
    // Preserve unknown entries (forward-compat) — never clobber the entries we own.
    // cache.aicm (the persisted-geometry mesh blob) is written STORED, not deflated: it is
    // large binary that barely compresses, and STORED lets doc_open extract it raw with
    // MZ_ZIP_FLAG_COMPRESSED_DATA — zero inflate AND zero CRC32 — which together dominated
    // open self-time (measured mz_crc32 47% + tinfl_decompress 34% of doc_open). Any zip
    // reader still reads a STORED entry transparently, so this stays fully back/forward
    // compatible; the cache is display-only + hash-gated, so the skipped zip CRC is fine.
    // cache.ckpt (CBOR + BinTools build checkpoints) follows the same rule.
    json manifest = cache_manifest(util::sha256_hex(document));
    for (const auto& [name, data] : extra) {
        if (owns_geometry_step && name == "cache.provenance") continue;
        if (is_known_entry(name, owns_geometry_step)) continue;
        // Optional caches: never persist one that the bounded reader would have to
        // discard before allocating its payload. The drop is reported to the caller.
        if (owns_geometry_step && is_optional_cache(name)) {
            if (data.size() > cache_budget) {
                if (dropped) dropped->push_back(name);
                continue;
            }
            cache_budget -= data.size();
        }
        // Optional caches + studies/*/results.npz are large binary blobs — STORED (no
        // deflate). A geometry cache above the reader's per-entry limit for COMPRESSED
        // entries is STORED too, so the reader admits it against the aggregate budget
        // instead of skipping it.
        const bool stored = preserved_meta || is_optional_cache(name) ||
                            (owns_geometry_step && is_geometry_cache(name) &&
                             data.size() > kMaxEntryExpandedBytes) ||
                            (name.rfind("studies/", 0) == 0 && name.find("/results.") != std::string::npos);
        add(name, data, /*stored=*/stored);
        if (owns_geometry_step && is_geometry_cache(name))
            manifest["members"][name] = util::sha256_hex(data);
    }
    if (owns_geometry_step && !manifest["members"].empty()) {
        std::string provenance;
        try { provenance = sign_manifest(manifest); }
        catch (...) { mz_zip_writer_end(&zip); throw; }
        add("cache.provenance", provenance, false);
    }

    if (!mz_zip_writer_finalize_archive(&zip)) {
        mz_zip_writer_end(&zip);
        io_error("doc_save: cannot finalize archive");
    }
    mz_zip_writer_end(&zip);

    if (std::fflush(stage.stream) != 0) io_error("doc_save: cannot flush archive");
#if defined(_WIN32)
    const auto destination = fs::absolute(out_path).make_preferred().native();
    const auto bytes = destination.size() * sizeof(wchar_t);
    // Win32 resolves FileName as a NUL-terminated path despite FileNameLength.
    std::vector<unsigned char> rename_buffer(
        offsetof(FILE_RENAME_INFO, FileName) + bytes + sizeof(wchar_t));
    auto* rename_info = reinterpret_cast<FILE_RENAME_INFO*>(rename_buffer.data());
    rename_info->ReplaceIfExists = !exclusive;
    rename_info->RootDirectory = nullptr;
    rename_info->FileNameLength = static_cast<DWORD>(bytes);
    std::memcpy(rename_info->FileName, destination.data(), bytes);
    const HANDLE file_handle = reinterpret_cast<HANDLE>(_get_osfhandle(_fileno(stage.stream)));
    if (!SetFileInformationByHandle(file_handle, FileRenameInfo, rename_info,
                                   static_cast<DWORD>(rename_buffer.size()))) {
        const DWORD error = GetLastError();
        io_error("doc_save: cannot publish archive (Windows error " + std::to_string(error) + ")");
    }
    stage.published = true;
#else
    const int result = exclusive
        ? ::linkat(stage.directory_fd, "container.tmp", AT_FDCWD, out_path.c_str(), 0)
        : ::renameat(stage.directory_fd, "container.tmp", AT_FDCWD, out_path.c_str());
    if (result != 0) io_error("doc_save: cannot publish archive");
#endif
    return native_policy::path_utf8(out_path);
}

std::string editable_container_save(const json& doc, const std::string& path,
                                    const std::map<std::string, std::string>& extra,
                                    const std::string& container, const std::string& kind,
                                    const std::string& format_name) {
    return editable_container_save_impl(doc, path, extra, container, kind, format_name);
}

std::string stepplus_save(const json& doc, const std::string& path,
                          const std::map<std::string, std::string>& extra,
                          std::vector<std::string>* dropped) {
    const std::string schema = doc.value("schema", std::string());
    const std::string kind = (schema == "aicad.asm/1") ? "assembly" : "part";
    return editable_container_save_impl(doc, path, extra, kContainer, kind, ".step+",
                                        nullptr, false, dropped);
}

std::size_t stepplus_optional_cache_budget(const json& doc,
                                           const std::map<std::string, std::string>& extra) {
    const std::string schema = doc.value("schema", std::string());
    const std::string kind = (schema == "aicad.asm/1") ? "assembly" : "part";
    static const std::size_t placeholder_bytes = placeholder_png().size();
    const auto th = extra.find("thumbnail.png");
    const std::size_t thumb_bytes = th != extra.end() ? th->second.size() : placeholder_bytes;
    return static_cast<std::size_t>(optional_cache_budget(
        editable_document_json(doc).size(), container_meta(doc, kContainer, kind).dump(2).size(),
        thumb_bytes, extra, /*owns_geometry_step=*/true));
}

// ---------------------------------------------------------------- shared editable-container open
enum class NativeReadMode { Open, Document, Component, Metadata, Relocate };

static StepPlusDoc editable_container_open_impl(const std::string& path,
                                    const std::string& expected_prefix,
                                    const std::string& format_name,
                                    NativeReadMode mode = NativeReadMode::Open) {
    const auto native_path = std::filesystem::u8path(path);
    if (!std::filesystem::exists(native_path)) io_error("doc_open: no such file: " + path);
    const bool owns_geometry_step = expected_prefix == "aicad.step+/";
    const bool read_only = mode == NativeReadMode::Document || mode == NativeReadMode::Component;

    NativeArchiveReader reader;
    if (!reader.open(native_path))
        io_error("doc_open: not a valid " + format_name + " (zip) container: " + path);
    auto& zip = reader.zip;

    const mz_uint count = mz_zip_reader_get_num_files(&zip);
    if (count > kMaxArchiveEntries) {
        mz_zip_reader_end(&zip);
        archive_limit_error("entry count limit", format_name);
    }
    mz_uint64 aggregate_expanded = 0;
    std::set<std::string> entry_names;
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) {
            mz_zip_reader_end(&zip);
            io_error("doc_open: invalid .step+ archive metadata");
        }
        if (mode != NativeReadMode::Open && !read_only && !entry_names.insert(st.m_filename).second) {
            mz_zip_reader_end(&zip);
            io_error("native file operation: duplicate archive entry");
        }
        if (mode != NativeReadMode::Open && !read_only &&
            mz_zip_reader_get_filename(&zip, i, nullptr, 0) > sizeof(st.m_filename))
            io_error("native file operation: archive entry name is too long to preserve");
        if (st.m_is_directory && mode != NativeReadMode::Relocate) continue;
        // Inspection reads only the two JSON members. Relocation drops stale caches
        // before allocating, but validates every payload it promises to preserve.
        if (mode == NativeReadMode::Metadata &&
            std::strcmp(st.m_filename, "meta.json") != 0 &&
            std::strcmp(st.m_filename, "document.json") != 0) continue;
        if (mode == NativeReadMode::Relocate && is_geometry_cache(st.m_filename)) continue;
        // Admit the optional geometry caches only after all required/unknown payloads have
        // passed their unchanged limits and reserved the aggregate byte budget. A cache that
        // is inadmissible is skipped (and reported) below — it never fails the open.
        if (owns_geometry_step && is_geometry_cache(st.m_filename))
            continue;
        // geometry.step is the interop snapshot every save rewrites from the rebuild; only
        // Relocate (which must preserve it verbatim) ever inflates it. Bounding a payload this
        // read never allocates made a large model's own save unopenable (a 78 MB snapshot of a
        // dense lofted duct tripped the 64 MB per-entry cap and failed doc_open).
        if (owns_geometry_step && mode != NativeReadMode::Relocate &&
            std::strcmp(st.m_filename, "geometry.step") == 0)
            continue;
        if (st.m_uncomp_size > kMaxEntryExpandedBytes) {
            mz_zip_reader_end(&zip);
            archive_limit_error("per-entry expanded byte limit", format_name);
        }
        if (st.m_uncomp_size > kMaxArchiveExpandedBytes ||
            aggregate_expanded > kMaxArchiveExpandedBytes - st.m_uncomp_size) {
            mz_zip_reader_end(&zip);
            archive_limit_error("aggregate expanded byte limit", format_name);
        }
        aggregate_expanded += st.m_uncomp_size;
        if (st.m_uncomp_size != 0 &&
            (st.m_comp_size == 0 || st.m_uncomp_size / st.m_comp_size > kMaxCompressionRatio)) {
            mz_zip_reader_end(&zip);
            archive_limit_error("compression ratio limit", format_name);
        }
    }

    auto read_entry = [&](const std::string& name, std::string& out) -> bool {
        const int idx = mz_zip_reader_locate_file(&zip, name.c_str(), nullptr, MZ_ZIP_FLAG_CASE_SENSITIVE);
        if (idx < 0) return false;
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, static_cast<mz_uint>(idx), &st)) return false;
        if (st.m_is_directory) return false;
        out.resize(static_cast<std::size_t>(st.m_uncomp_size));
        if (out.empty()) return true;
        return mz_zip_reader_extract_to_mem(&zip, static_cast<mz_uint>(idx), out.data(), out.size(),
                                            0) != 0;
    };

    StepPlusDoc result;

    std::string meta_bytes;
    if (!read_entry("meta.json", meta_bytes)) {
        mz_zip_reader_end(&zip);
        io_error("not a " + format_name + " container: meta.json missing");
    }
    json meta;
    try {
        meta = json::parse(meta_bytes);
    } catch (const std::exception& e) {
        mz_zip_reader_end(&zip);
        io_error(std::string("doc_open: bad meta.json: ") + e.what());
    }
    const std::string container = meta.value("container", std::string());
    if (container.rfind(expected_prefix, 0) != 0) {
        mz_zip_reader_end(&zip);
        io_error("unknown container '" + container + "'");
    }
    const std::string major =
        (container.find('/') != std::string::npos) ? container.substr(container.rfind('/') + 1) : "";
    if (!major.empty() && major != "1") {
        mz_zip_reader_end(&zip);
        io_error("unsupported " + format_name + " major version '" + major + "'");
    }

    std::string doc_bytes;
    if (!read_entry("document.json", doc_bytes)) {
        mz_zip_reader_end(&zip);
        io_error("not a " + format_name + " container: document.json missing");
    }
    try {
        result.doc = json::parse(doc_bytes);
    } catch (const std::exception& e) {
        mz_zip_reader_end(&zip);
        io_error(std::string("doc_open: bad document.json: ") + e.what());
    }
    if (owns_geometry_step && native_policy::rooted()) {
        const std::string base =
            native_policy::path_utf8(std::filesystem::absolute(native_path).parent_path());
        try {
            if (result.doc.contains("components") && result.doc["components"].is_array())
                for (const auto& component : result.doc["components"])
                    if (component.contains("source") && component["source"].is_object() &&
                        component["source"].contains("path"))
                        native_policy::resolve_reference(
                            component["source"]["path"].get<std::string>(), base);
            if (result.doc.contains("features") && result.doc["features"].is_array())
                for (const auto& feature : result.doc["features"]) {
                    if (feature.value("type", std::string()) != "import") continue;
                    const auto& p = feature.contains("params") ? feature["params"] : feature;
                    if (p.contains("source_path") && p["source_path"].is_string() &&
                        !p["source_path"].get_ref<const std::string&>().empty())
                        native_policy::resolve_reference(p["source_path"].get<std::string>());
                }
        } catch (const std::exception& e) {
            mz_zip_reader_end(&zip);
            throw OpError("io_error", std::string("document reference refused: ") + e.what());
        }
    }
    if (mode == NativeReadMode::Metadata || mode == NativeReadMode::Document ||
        (mode == NativeReadMode::Component &&
         result.doc.value("schema", std::string()) == "aicad.asm/1")) {
        mz_zip_reader_end(&zip);
        return result;
    }
    if (mode == NativeReadMode::Relocate) result.extra.emplace("meta.json", meta_bytes);

    // Carry every entry we don't own back onto the session for forward-compat resave.
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) continue;
        const std::string name = st.m_filename;
        if (st.m_is_directory && mode != NativeReadMode::Relocate) continue;
        if (mode == NativeReadMode::Component &&
            name != "cache.json" && name != "cache.brep" && name != "cache.provenance") continue;
        if (mode == NativeReadMode::Relocate) {
            if (name == "meta.json" || name == "document.json") continue;
            if (is_geometry_cache(name) || name == "cache.provenance") {
                result.warnings.push_back("invalidated:" + name);
                continue;
            }
        } else if (is_known_entry(name, owns_geometry_step)) continue;
        if (owns_geometry_step && is_geometry_cache(name)) {
            // A STORED cache cannot expand during decompression. It may use the
            // remaining bounded archive budget; compressed caches retain the
            // ordinary per-entry and compression-ratio caps.
            const bool stored = st.m_method == 0 && st.m_comp_size == st.m_uncomp_size;
            const mz_uint64 entry_limit = stored ? kMaxArchiveExpandedBytes : kMaxEntryExpandedBytes;
            const char* refused = nullptr;
            if (st.m_uncomp_size > entry_limit) refused = "entry_limit";
            else if (st.m_uncomp_size > kMaxArchiveExpandedBytes - aggregate_expanded)
                refused = "aggregate_limit";
            else if (st.m_uncomp_size != 0 && (st.m_comp_size == 0 ||
                         st.m_uncomp_size / st.m_comp_size > kMaxCompressionRatio))
                refused = "compression_ratio";
            if (refused) {
                result.skipped[name] = refused;
                continue;
            }
            aggregate_expanded += st.m_uncomp_size;
        }
        // STORED entries (method 0 — e.g. cache.aicm) carry the persisted-geometry mesh. Extract
        // them raw with MZ_ZIP_FLAG_COMPRESSED_DATA so miniz skips BOTH inflate and CRC; for a
        // STORED entry the compressed bytes are the data. Allocate the destination exactly once.
        const mz_uint flags = ((mode == NativeReadMode::Open || read_only) && st.m_method == 0)
                                 ? MZ_ZIP_FLAG_COMPRESSED_DATA : 0;
        std::string data(static_cast<std::size_t>(st.m_uncomp_size), '\0');
        if (!data.empty() &&
            !mz_zip_reader_extract_to_mem(&zip, i, data.data(), data.size(), flags)) {
            if (mode == NativeReadMode::Relocate) {
                mz_zip_reader_end(&zip);
                io_error("native relocate: cannot preserve entry '" + name + "'");
            }
            if (owns_geometry_step && is_geometry_cache(name)) result.skipped[name] = "extract_failed";
            continue;
        }
        result.extra.emplace(name, std::move(data));
    }

    mz_zip_reader_end(&zip);
    return result;
}

StepPlusDoc editable_container_open(const std::string& path,
                                    const std::string& expected_prefix,
                                    const std::string& format_name) {
    return editable_container_open_impl(path, expected_prefix, format_name);
}

StepPlusDoc stepplus_open(const std::string& path) {
    return editable_container_open(path, "aicad.step+/", ".step+");
}

json stepplus_read_document(const std::string& path) {
    return editable_container_open_impl(path, "aicad.step+/", ".step+",
                                        NativeReadMode::Document).doc;
}

StepPlusDoc stepplus_open_component(const std::string& path) {
    return editable_container_open_impl(path, "aicad.step+/", ".step+",
                                        NativeReadMode::Component);
}

namespace {
namespace fs = std::filesystem;
constexpr std::size_t kMaxNativeFiles = 4096;
constexpr std::size_t kMaxNativePathBytes = 32768;

[[noreturn]] void native_invalid(const std::string& message) {
    throw OpError("invalid_argument", "native file operation: " + message);
}

std::string native_string(const json& value, const char* field) {
    if (!value.is_string()) native_invalid(std::string(field) + " must be a string");
    const auto& text = value.get_ref<const std::string&>();
    if (text.empty() || text.size() > kMaxNativePathBytes ||
        text.find('\0') != std::string::npos)
        native_invalid(std::string(field) + " must be nonempty and bounded without NUL");
    return text;
}

fs::path native_canonical(const fs::path& path) {
    auto canonical = fs::weakly_canonical(path);
#if defined(_WIN32)
    // MSVC's weakly_canonical keeps the spelling of existing components:
    // C:\WINDOWS and C:\Windows, and 8.3 aliases, can name the same directory.
    // Expand the existing prefix; GetLongPathName cannot expand a new leaf.
    fs::path existing = canonical, suffix;
    while (!fs::exists(existing) && existing != existing.root_path()) {
        suffix = suffix.empty() ? existing.filename() : existing.filename() / suffix;
        existing = existing.parent_path();
    }
    const auto spelling = existing.native();
    const DWORD size = GetLongPathNameW(spelling.c_str(), nullptr, 0);
    if (!size)
        throw fs::filesystem_error("native path normalization", existing,
                                  std::error_code(GetLastError(), std::system_category()));
    std::wstring expanded(size, L'\0');
    const DWORD written = GetLongPathNameW(spelling.c_str(), expanded.data(), size);
    if (!written || written >= size)
        throw fs::filesystem_error("native path normalization", existing,
                                  std::error_code(GetLastError(), std::system_category()));
    expanded.resize(written);
    // GetLongPathName preserves the caller's drive-letter case.
    if (expanded.size() >= 2 && expanded[1] == L':' &&
        expanded[0] >= L'a' && expanded[0] <= L'z')
        expanded[0] += L'A' - L'a';
    canonical = fs::path(expanded);
    if (!suffix.empty()) canonical /= suffix;
#endif
    return canonical;
}

fs::path native_absolute(const json& value, const char* field) {
    const auto path = fs::u8path(native_string(value, field));
    if (!path.is_absolute()) native_invalid(std::string(field) + " must be absolute");
    return native_canonical(path);
}

std::string native_fold(std::string text) {
    for (char& c : text) if (c >= 'A' && c <= 'Z') c += 'a' - 'A';
    return text;
}

// Check components as well as the leaf: "Parts/X" and existing "parts/x" must
// not become two different files merely because this host is case-sensitive.
void native_destination_available(const fs::path& destination) {
    const auto same_folded = [](auto left, auto right) {
        if (left >= 'A' && left <= 'Z') left += 'a' - 'A';
        if (right >= 'A' && right <= 'Z') right += 'a' - 'A';
        return left == right;
    };
    fs::path parent = destination.root_path();
    for (const auto& component : destination.relative_path()) {
        if (fs::is_directory(parent)) {
            const auto& component_name = component.native();
            for (const auto& sibling : fs::directory_iterator(parent)) {
                // Unrelated NTFS names can contain unpaired UTF-16 surrogates.
                // Compare native code units without narrowing or allocating UTF-8.
                const auto sibling_name = sibling.path().filename();
                const auto& name = sibling_name.native();
                if (name != component_name &&
                    std::equal(name.begin(), name.end(), component_name.begin(),
                               component_name.end(), same_folded)) {
                    // An existing ancestor's alternate spelling is not a second
                    // destination (Windows system/temp paths commonly use it).
                    // Leaves remain occupied, and distinct case-sensitive
                    // directories still collide for portable relocation.
                    std::error_code ec;
                    const auto requested = parent / component;
                    if (requested != destination && fs::is_directory(requested) &&
                        fs::equivalent(requested, sibling.path(), ec) && !ec)
                        continue;
                    native_invalid("case-colliding destination: " + native_policy::path_utf8(destination));
                }
            }
        }
        parent /= component;
    }
    // symlink_status also rejects dangling symlinks, which exists() alone misses.
    if (fs::exists(fs::symlink_status(destination)))
        native_invalid("destination already exists: " + native_policy::path_utf8(destination));
}

std::string native_pointer_token(const std::string& token) {
    std::string escaped;
    for (char c : token) {
        if (c == '~') escaped += "~0";
        else if (c == '/') escaped += "~1";
        else escaped += c;
    }
    return escaped;
}

struct NativeReferences {
    json dependencies = json::array();
    std::set<std::string> recognized;
};

void native_reference(NativeReferences& refs, const json& value,
                      const std::string& pointer, const std::string& kind,
                      const fs::path& parent, bool supported = true,
                      const std::string& reason = {}, bool required = true) {
    refs.recognized.insert(pointer);
    json row{{"json_pointer", pointer}, {"kind", kind}, {"literal", value},
             {"supported", supported}, {"required", required}};
    if (!reason.empty()) row["reason"] = reason;
    if (!required) row["usage"] = "embedded_provenance";
    if (!value.is_string() || value.get_ref<const std::string&>().empty()) {
        row["supported"] = false;
        row["reason"] = "file reference must be a nonempty string";
    } else {
        const auto& literal = value.get_ref<const std::string&>();
        // Embedded imports retain historical URI provenance verbatim. It is not a
        // file dependency and is never opened, rebased, or passed through path_map.
        if (supported && !required && literal.size() <= kMaxNativePathBytes &&
            literal.find('\0') == std::string::npos && literal.find("://") != std::string::npos) {
            row["format"] = "uri";
            row["rewrite"] = false;
        } else if (literal.size() > kMaxNativePathBytes || literal.find('\0') != std::string::npos ||
                   literal.find("://") != std::string::npos) {
            row["supported"] = false;
            row["reason"] = "unsupported URI or foreign-platform path";
        } else if (supported) {
            const auto path = fs::u8path(literal);
#if defined(_WIN32)
            // Drive-qualified/UNC absolute paths and backslash separators are
            // native here. Drive-relative or root-only paths lack a stable base.
            const bool foreign = path.has_root_path() && !path.is_absolute();
#else
            const bool foreign = literal.find('\\') != std::string::npos ||
                                 (literal.size() > 1 && literal[1] == ':');
#endif
            if (foreign) {
                row["supported"] = false;
                row["reason"] = "unsupported URI or foreign-platform path";
            } else {
                const auto resolved = native_canonical(path.is_absolute() ? path : parent / path);
                row["resolved_path"] = native_policy::path_utf8(resolved);
                row["exists"] = fs::exists(resolved);
                row["format"] = "filesystem";
                row["rewrite"] = true;
            }
        }
    }
    refs.dependencies.push_back(std::move(row));
}

// Inventory unknown path-bearing fields without interpreting arbitrary metadata as
// a native source schema. In particular an opaque embedded source is not a pathname.
void native_unknown_references(const json& value, const std::string& pointer,
                               const fs::path& parent, NativeReferences& refs,
                               unsigned depth = 0) {
    if (depth > 64) native_invalid("document nesting exceeds 64");
    if (value.is_object()) {
        for (const auto& [key, child] : value.items()) {
            const auto at = pointer + "/" + native_pointer_token(key);
            if (refs.recognized.count(at)) continue;
            // Feature refs.path is a curve/datum selector (sweep/surf_sweep), not
            // an external file. Do not misclassify geometric reference syntax.
            if (key == "refs") continue;
            if (key == "path" || key == "source_path" || key == "base_dir" ||
                key == "source_base_dir" || key == "snapshot_base_dir") {
                if (pointer == "/meta" || pointer.compare(0, 6, "/meta/") == 0) {
                    // Free-form document metadata records provenance, not geometry
                    // inputs. Preserve historical paths (including foreign paths)
                    // verbatim rather than rewriting evidence or blocking a move.
                    refs.dependencies.push_back({
                        {"json_pointer", at}, {"kind", "metadata"}, {"literal", child},
                        {"supported", true}, {"required", false}, {"rewrite", false},
                        {"usage", "documentary_metadata"}});
                } else {
                    native_reference(refs, child, at, "unknown", parent, false,
                                     "path field outside a supported native dependency schema");
                }
            } else {
                native_unknown_references(child, at, parent, refs, depth + 1);
            }
        }
    } else if (value.is_array()) {
        for (std::size_t i = 0; i < value.size(); ++i)
            native_unknown_references(value[i], pointer + "/" + std::to_string(i),
                                      parent, refs, depth + 1);
    } else if (value.is_number_float() && !std::isfinite(value.get<double>())) {
        native_invalid("document contains a non-finite number");
    }
}

NativeReferences native_references(const json& doc, const fs::path& parent) {
    if (!doc.is_object()) native_invalid("document.json must be an object");
    const auto schema = doc.value("schema", std::string());
    if (schema != "aicad.part/1" && schema != "aicad.asm/1")
        native_invalid("unsupported document schema: " + schema);
    NativeReferences refs;
    if (doc.contains("components")) {
        if (!doc["components"].is_array()) native_invalid("components must be an array");
        for (std::size_t i = 0; i < doc["components"].size(); ++i) {
            const auto& component = doc["components"][i];
            const auto prefix = "/components/" + std::to_string(i) + "/source";
            if (!component.is_object() || !component.contains("source") ||
                !component["source"].is_object()) {
                native_reference(refs, nullptr, prefix, "component", parent, false,
                                 "component source must be an object");
                continue;
            }
            const auto& source = component["source"];
            if (source.contains("path"))
                native_reference(refs, source["path"], prefix + "/path", "component", parent);
            if (source.contains("embedded"))
                native_reference(refs, source["embedded"], prefix + "/embedded",
                                 "embedded_component", parent, false,
                                 "opaque embedded component source has no supported relocation base");
            if (!source.contains("path") && !source.contains("embedded"))
                native_reference(refs, source, prefix, "component", parent, false,
                                 "unknown component source format");
        }
    }
    if (doc.contains("features")) {
        if (!doc["features"].is_array()) native_invalid("features must be an array");
        for (std::size_t i = 0; i < doc["features"].size(); ++i) {
            const auto& feature = doc["features"][i];
            if (!feature.is_object()) native_invalid("feature must be an object");
            if (feature.value("type", std::string()) != "import") continue;
            if (!feature.contains("params") || !feature["params"].is_object())
                native_invalid("import params must be an object");
            const bool embedded = feature["params"].contains("embedded_brep") &&
                feature["params"]["embedded_brep"].is_string() &&
                !feature["params"]["embedded_brep"].get_ref<const std::string&>().empty();
            for (const char* key : {"source_path", "path"}) {
                if (feature["params"].contains(key))
                    native_reference(refs, feature["params"][key],
                        "/features/" + std::to_string(i) + "/params/" + key, "import", parent,
                        true, {}, !embedded);
            }
        }
    }
    native_unknown_references(doc, "", parent, refs);
    return refs;
}

json native_metadata(const json& doc, const fs::path& path, const NativeReferences& refs) {
    json features = json::array(), components = json::array();
    if (doc.contains("features")) for (const auto& feature : doc["features"]) {
        json row = json::object();
        for (const char* key : {"id", "type", "name", "label", "suppressed"})
            if (feature.contains(key)) row[key] = feature[key];
        features.push_back(std::move(row));
    }
    if (doc.contains("components")) for (const auto& component : doc["components"]) {
        json row = json::object();
        for (const char* key : {"id", "name", "source", "transform", "fixed", "suppressed"})
            if (component.contains(key)) row[key] = component[key];
        components.push_back(std::move(row));
    }
    return {{"ok", true}, {"path", native_policy::path_utf8(path)}, {"schema", doc.at("schema")},
            {"name", doc.value("name", json())}, {"document_id", doc.value("id", json())},
            {"document_hash", stepplus_doc_hash(doc)},
            {"document_hash_algorithm", "native-canonical-fnv64-excluding-root-id"},
            {"features", std::move(features)}, {"components", std::move(components)},
            {"dependencies", refs.dependencies}};
}

// This is an invariant hash, NOT a geometry hash: mask only the explicitly mutable
// display names and supported filesystem references. All IDs, feature labels,
// embedded model data, finishes, mates, frames and placements remain represented.
std::string fnv1a_hex(const std::string& s);
std::string native_immutable_hash(json doc, const json& dependencies) {
    doc.erase("name");
    if (doc.contains("components"))
        for (auto& component : doc["components"]) component.erase("name");
    for (const auto& dependency : dependencies)
        if (dependency.value("supported", false) && dependency.value("rewrite", false))
            doc[json::json_pointer(dependency["json_pointer"].get<std::string>())] = nullptr;
    return fnv1a_hex(editable_document_json(doc));
}

struct NativeStamp {
    uintmax_t size;
    fs::file_time_type modified;
};
NativeStamp native_stamp(const fs::path& path) {
    if (!fs::is_regular_file(path))
        native_invalid("source is not a regular file: " + native_policy::path_utf8(path));
    return {fs::file_size(path), fs::last_write_time(path)};
}
void native_check_stamp(const fs::path& path, const NativeStamp& stamp) {
    const auto current = native_stamp(path);
    if (current.size != stamp.size || current.modified != stamp.modified)
        io_error("native relocate: source changed during operation: " + native_policy::path_utf8(path));
}
}  // namespace

json native_file_operations(const json& params) {
    json result{{"ok", false}, {"action", nullptr}, {"files", json::array()},
                {"failures", json::array()}};
    auto failure = [&](const std::exception& error, const std::string& path = {}) {
        json row{{"ok", false}, {"message", error.what()}};
        if (!path.empty()) row["path"] = path;
        const auto* op = dynamic_cast<const OpError*>(&error);
        row["code"] = op ? op->code : "io_error";
        result["failures"].push_back(row);
        return row;
    };
    try {
        if (!params.is_object() || !params.contains("action")) native_invalid("action is required");
        const auto action = native_string(params["action"], "action");
        result["action"] = action;
        if (action == "inspect") {
            if (!params.contains("paths") || !params["paths"].is_array() ||
                params["paths"].empty() || params["paths"].size() > kMaxNativeFiles)
                native_invalid("paths must contain 1..4096 absolute filenames");
            for (const auto& value : params["paths"]) {
                std::string original;
                try {
                    original = native_string(value, "path");
                    const auto path = native_absolute(value, "path");
                    const auto stamp = native_stamp(path);
                    const auto loaded = editable_container_open_impl(
                        native_policy::path_utf8(path), "aicad.step+/", ".step+", NativeReadMode::Metadata);
                    const auto refs = native_references(loaded.doc, path.parent_path());
                    auto row = native_metadata(loaded.doc, path, refs);
                    row["immutable_document_hash"] = native_immutable_hash(loaded.doc, refs.dependencies);
                    row["immutable_document_hash_algorithm"] =
                        "native-canonical-fnv64-masked-display-names-and-file-references";
                    native_check_stamp(path, stamp);
                    row["original_path"] = original;
                    result["files"].push_back(std::move(row));
                } catch (const std::exception& error) {
                    result["files"].push_back(failure(error, original));
                }
            }
        } else if (action == "relocate") {
            if (!params.contains("entries") || !params["entries"].is_array() ||
                params["entries"].empty() || params["entries"].size() > kMaxNativeFiles)
                native_invalid("entries must contain 1..4096 relocation objects");
            struct Entry {
                fs::path path, destination;
                NativeStamp stamp;
                json options;
                std::string document_hash;
            };
            std::vector<Entry> entries;
            std::map<std::string, std::string> path_map;
            std::set<std::string> sources, destinations;
            std::map<std::string, std::string> destination_components;
            if (params.contains("path_map")) {
                if (!params["path_map"].is_object() || params["path_map"].size() > kMaxNativeFiles)
                    native_invalid("path_map must be an object with at most 4096 entries");
                for (const auto& [old_path, new_path] : params["path_map"].items()) {
                    const auto old_key = native_policy::path_utf8(native_absolute(old_path, "path_map source"));
                    const auto new_key = native_policy::path_utf8(native_absolute(new_path, "path_map destination"));
                    if (!path_map.emplace(old_key, new_key).second)
                        native_invalid("duplicate canonical path_map source");
                }
            }
            // Complete the plan preflight before any directory or archive is written.
            for (const auto& item : params["entries"]) {
                if (!item.is_object() || !item.contains("path") || !item.contains("destination"))
                    native_invalid("each entry requires path and destination");
                const auto path = native_absolute(item["path"], "path");
                native_destination_available(fs::u8path(native_string(item["destination"], "destination")));
                const auto destination = native_absolute(item["destination"], "destination");
                const auto path_text = native_policy::path_utf8(path);
                const auto destination_text = native_policy::path_utf8(destination);
                if (!sources.insert(native_fold(path_text)).second)
                    native_invalid("duplicate source: " + path_text);
                if (!destinations.insert(native_fold(destination_text)).second)
                    native_invalid("duplicate or case-colliding destination: " + destination_text);
                if (native_fold(path_text) == native_fold(destination_text))
                    native_invalid("source and destination must differ");
                native_destination_available(destination);
                if (item.contains("name")) native_string(item["name"], "name");
                if (item.contains("component_names")) {
                    if (!item["component_names"].is_object() ||
                        item["component_names"].size() > kMaxNativeFiles)
                        native_invalid("component_names must be a bounded object");
                    for (const auto& [id, name] : item["component_names"].items()) {
                        native_string(id, "component id");
                        native_string(name, "component name");
                    }
                }
                const auto inserted = path_map.emplace(path_text, destination_text);
                if (!inserted.second && inserted.first->second != destination_text)
                    native_invalid("path_map disagrees with relocation entry: " + path_text);
                entries.push_back({path, destination, native_stamp(path), item, {}});
            }
            for (const auto& entry : entries) {
                fs::path prefix = entry.destination.root_path();
                for (const auto& component : entry.destination.relative_path()) {
                    prefix /= component;
                    const auto spelling = native_policy::path_utf8(prefix);
                    const auto folded = native_fold(spelling);
                    const auto inserted = destination_components.emplace(folded, spelling);
                    if (!inserted.second && inserted.first->second != spelling)
                        native_invalid("case-colliding planned destination directories: " + spelling);
                    if (prefix != entry.destination && destinations.count(folded))
                        native_invalid("planned destination is also a parent directory: " + spelling);
                }
            }
            std::set<std::string> mapped_destinations;
            for (const auto& [source, destination] : path_map) {
                if (native_fold(source) == native_fold(destination) ||
                    sources.count(native_fold(destination)) ||
                    !mapped_destinations.insert(native_fold(destination)).second)
                    native_invalid("path_map contains same-file, source overwrite, or colliding targets");
                // A mapping may name a copy in a later batch. Closure is reported
                // by inspection, not guessed or enforced by loading dependencies.
            }
            // Inspect all source trees first: unsupported references or misspelled
            // component IDs must not leave an avoidable half-relocated batch.
            // Retain only bounded hash/stamp records, never document trees or
            // embedded BREP across files. Large libraries remain one-file-at-a-time.
            for (auto& entry : entries) {
                auto loaded = editable_container_open_impl(native_policy::path_utf8(entry.path), "aicad.step+/",
                                                           ".step+", NativeReadMode::Metadata);
                auto refs = native_references(loaded.doc, entry.path.parent_path());
                for (const auto& dependency : refs.dependencies)
                    if (!dependency.value("supported", false))
                        native_invalid("unsupported dependency in " + native_policy::path_utf8(entry.path) +
                                       " at " + dependency["json_pointer"].get<std::string>());
                if (entry.options.contains("component_names")) {
                    std::set<std::string> ids;
                    if (loaded.doc.contains("components"))
                        for (const auto& c : loaded.doc["components"])
                            if (c.contains("id") && c["id"].is_string()) ids.insert(c["id"].get<std::string>());
                    for (const auto& [id, name] : entry.options["component_names"].items())
                        if (!ids.count(id)) native_invalid("unknown component id: " + id);
                }
                entry.document_hash = fnv1a_hex(editable_document_json(loaded.doc));
                native_check_stamp(entry.path, entry.stamp);
            }
            for (auto& entry : entries) {
                json row{{"ok", false}, {"path", native_policy::path_utf8(entry.path)},
                         {"original_path", entry.options["path"]},
                         {"destination", native_policy::path_utf8(entry.destination)}, {"status", "not_written"}};
                try {
                    native_check_stamp(entry.path, entry.stamp);
                    native_destination_available(entry.destination);
                    auto loaded = editable_container_open_impl(native_policy::path_utf8(entry.path), "aicad.step+/",
                                                               ".step+", NativeReadMode::Relocate);
                    if (fnv1a_hex(editable_document_json(loaded.doc)) != entry.document_hash)
                        io_error("native relocate: source document changed");
                    const auto dependencies = native_references(loaded.doc, entry.path.parent_path()).dependencies;
                    row["source_document_hash"] = stepplus_doc_hash(loaded.doc);
                    const auto immutable_hash = native_immutable_hash(loaded.doc, dependencies);
                    if (entry.options.contains("name")) loaded.doc["name"] = entry.options["name"];
                    if (entry.options.contains("component_names") && loaded.doc.contains("components"))
                        for (auto& c : loaded.doc["components"]) {
                            const auto id = c.value("id", std::string());
                            if (entry.options["component_names"].contains(id))
                                c["name"] = entry.options["component_names"][id];
                        }
                    json rewrites = json::array();
                    for (const auto& dependency : dependencies) {
                        if (!dependency.value("rewrite", false)) continue;
                        const auto resolved = dependency["resolved_path"].get<std::string>();
                        const auto mapped = path_map.find(resolved);
                        const auto target = fs::u8path(mapped == path_map.end() ? resolved : mapped->second);
                        const auto relative = target.lexically_relative(entry.destination.parent_path());
                        const auto replacement = relative.empty() ? native_policy::path_utf8(target)
                                                                 : native_policy::generic_path_utf8(relative);
                        loaded.doc[json::json_pointer(dependency["json_pointer"].get<std::string>())] = replacement;
                        auto rewrite = dependency;
                        rewrite["destination_literal"] = replacement;
                        rewrite["destination_resolved_path"] = native_policy::path_utf8(target);
                        rewrites.push_back(std::move(rewrite));
                    }
                    if (native_immutable_hash(loaded.doc, dependencies) != immutable_hash)
                        io_error("native relocate: immutable document invariant changed");
                    row["immutable_document_hash"] = immutable_hash;
                    row["immutable_document_hash_algorithm"] = "native-canonical-fnv64-masked-display-names-and-file-references";
                    row["document_hash"] = stepplus_doc_hash(loaded.doc);
                    row["document_hash_algorithm"] = "native-canonical-fnv64-excluding-root-id";
                    row["dependency_rewrites"] = std::move(rewrites);
                    row["invalidated_caches"] = json::array();
                    for (const auto& warning : loaded.warnings)
                        if (warning.rfind("invalidated:", 0) == 0)
                            row["invalidated_caches"].push_back(warning.substr(12));
                    json preserved = json::array();
                    for (const auto& [name, bytes] : loaded.extra)
                        if (name != "meta.json")
                            preserved.push_back({{"name", name}, {"bytes", bytes.size()},
                                                 {"native_fnv64", fnv1a_hex(bytes)}});
                    row["preserved_entries"] = std::move(preserved);
                    const auto meta = json::parse(loaded.extra.at("meta.json"));
                    native_check_stamp(entry.path, entry.stamp);
                    editable_container_save_impl(loaded.doc, native_policy::path_utf8(entry.destination), loaded.extra,
                        kContainer, loaded.doc["schema"] == "aicad.asm/1" ? "assembly" : "part",
                        ".step+", &meta, true);
                    row["status"] = "written";
                    // If an outside writer races publication, keep the new copy and
                    // report it as written-but-failed rather than concealing the output.
                    native_check_stamp(entry.path, entry.stamp);
                    row["ok"] = true;
                } catch (const std::exception& error) {
                    auto failed = failure(error, native_policy::path_utf8(entry.path));
                    row["code"] = failed["code"];
                    row["message"] = failed["message"];
                    if (row["status"] != "written") row["status"] = "failed";
                }
                result["files"].push_back(std::move(row));
            }
        } else native_invalid("action must be inspect or relocate");
    } catch (const std::exception& error) {
        failure(error);
        if (result["action"] == "relocate" && result["files"].empty() &&
            params.contains("entries") && params["entries"].is_array() &&
            params["entries"].size() <= kMaxNativeFiles) {
            for (const auto& item : params["entries"]) {
                json row{{"ok", false}, {"status", "not_written"}, {"reason", "preflight_failed"}};
                if (item.is_object()) {
                    if (item.contains("path")) row["path"] = item["path"];
                    if (item.contains("destination")) row["destination"] = item["destination"];
                }
                result["files"].push_back(std::move(row));
            }
        }
    }
    result["ok"] = result["failures"].empty();
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
    // the volatile top-level "id": doc_open re-stamps it to the new session id BEFORE the hash
    // check, and it never affects geometry — leaving it in would miss every cache. Nested "id"
    // keys remain part of the hash.
    return fnv1a_hex(canonical_doc_json(doc, "id"));
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
        if (pos > blob.size() || blob.size() - pos < 8)
            throw bad(std::string("truncated ") + what);
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
        if (pos > blob.size() || len > blob.size() - pos) throw bad("truncated body");
        std::istringstream is(blob.substr(pos, static_cast<std::size_t>(len)), std::ios::binary);
        pos += static_cast<std::size_t>(len);
        TopoDS_Shape sh;
        BinTools::Read(sh, is);
        if (sh.IsNull()) throw bad("null shape");
        bodies.push_back(sh);
    }
    return bodies;
}

// --- geometry.step (contracts/stepplus.md §1) ---------------------------------------
std::string stepplus_geometry_step_bytes(const std::vector<TopoDS_Shape>& bodies) {
    if (bodies.empty()) return std::string();
    try {
        STEPControl_Writer writer;
        // Pin the contracted schema + unit on the transfer instead of relying on the
        // process-wide Interface_Static defaults: the container promises AP214 in mm, and a
        // host that set write.step.schema for an interchange export must not be able to
        // change what a NATIVE save embeds. Both values happen to be the DESTEP defaults.
        DESTEP_Parameters params;
        params.WriteSchema = DESTEP_Parameters::WriteMode_StepSchema_AP214IS;
        params.WriteUnit = UnitsMethods_LengthUnit_Millimeter;
        std::size_t transferred = 0;
        for (const TopoDS_Shape& body : bodies) {
            if (body.IsNull()) continue;
            bool any_solid = false;
            // One SOLID per transfer (mirrors export_bodies): a boolean-result body can be a
            // compound of DISJOINT lumps, and transferring that compound AsIs emits a single
            // malformed manifold_solid_brep that re-reads as one invalid solid.
            for (TopExp_Explorer ex(body, TopAbs_SOLID); ex.More(); ex.Next()) {
                if (writer.Transfer(ex.Current(), STEPControl_AsIs, params) != IFSelect_RetDone)
                    return std::string();
                any_solid = true;
                ++transferred;
            }
            if (any_solid) continue;
            if (writer.Transfer(body, STEPControl_AsIs, params) != IFSelect_RetDone)
                return std::string();
            ++transferred;
        }
        if (transferred == 0) return std::string();
        std::ostringstream os(std::ios::binary);
        if (writer.WriteStream(os) != IFSelect_RetDone) return std::string();
        return os.str();
    } catch (...) {
        return std::string();  // honest drop: a writer problem never fails the save
    }
}

std::string stepplus_cache_engine_key() {
    // This is a geometry-semantic epoch, not just a serialization version. Bump it on
    // ANY compiler change that can alter geometry OR topology: the same document hash
    // and OCCT version do not prove that an older compiler produced the current faces.
    // /2 invalidates pre-fix centered:false placement (bbox tolerance padding), whose
    // nearly-coplanar split faces disagree with selectors from a fresh feature rebuild.
    // Both display replay and assembly BREP loading must reject that old snapshot.
    // /3 invalidates display snapshots from before selected-face finishes and the
    // shared cylindrical bore/boss fingerprint. Old cached colors cannot override
    // the authored face metadata retained in document.json.
    // /4 binds finishes to exact fingerprints even when their short SIDs collide.
    // /5 unifies same-domain faces after every feature-level boolean (primitive, extrude,
    // loft, revolve, sweep, rib, boolean, mirror; boss and cut); older snapshots carry the
    // split seams.
    // /6 re-attributes face/edge owners through op histories (a dress-up feature owns only
    // the faces it generates); cached display responses carry the old owner_map.
    // /7 builds closed mesh imports directly and merges coplanar triangles into planar
    // faces; older source-mesh snapshots carry per-triangle topology.
    // /8 takes planar outward normals from geom/face_normal.h (left-handed plane frames —
    // revolve end faces — read inward before): hole drill direction, weld/text frames and
    // selector normals on such faces change.
    // /9 shell validates every offset strategy and walks the whole join/tolerance ladder
    // (a freeform body can now shell where the first, invalid result used to be refused),
    // and face-list slots resolve each geometry-query element (shell remove top+bottom).
    // /10 ranks geometry-query `near` by min(distance to the trimmed sub-shape, distance to
    // its centroid) instead of centroid distance alone (doc/picks.cpp rank_near): a point ON
    // a large circle/face now picks it over a small entity whose centroid sits closer.
    // /11 shells a tube-like body the offset ladder refuses (a rectangle-to-circle loft's
    // crease-to-tangent seams) through geom/shell_sections.cpp's sectional builder: such a
    // shell used to fail the feature and now builds a wall.
    // /12 profile region picks never snap: a centroid outside its region's grown bbox, two
    // picks on one region, or an equidistant hintless pick fails the feature (doc/document.cpp
    // resolve_profile_faces). A cache built by a silent snap must re-resolve and report it.
    // /13 DXF/DWG/SAT imports carry exact ACIS solids/surfaces and keep their baked body
    // grouping (a drawing profile stays one body); older snapshots lack those bodies.
    // /14 refuses closed but BRep-invalid, collapsed or self-intersecting dress results;
    // old cached blends must rebuild through the geometry-validity acceptance gate.
    // /15 a fillet on an edge shared by two bodies (line contact) fills the concave corners
    // and joins them into one solid; caches holding the old failed fillet must rebuild.
    // /16 UnifySameDomain runs on a topology copy (it edited the boolean's shared input
    // TShapes in place, corrupting cached checkpoints) and a cut that grows the exact
    // volume fails; caches/checkpoints built by the mutating unify may be invalid.
    // /17 sketcher overhaul: oblique linear edges project as one analytic line (not a
    // sampled BSpline run), composite rectangle/polygon entities carry relations, and the
    // 2D solver applies the extended relation set (and no longer applies wrong-type
    // residuals), so converted geometry and solved sketch coordinates can differ.
    static constexpr const char* kCacheSchema = "chudware-geomcache/17";
#ifdef OCC_VERSION_COMPLETE
    return std::string(kCacheSchema) + ";occt=" OCC_VERSION_COMPLETE;
#else
    return std::string(kCacheSchema) + ";occt=none";
#endif
}

// --- build-state write-back (contracts/stepplus.md §17) -----------------------------
NativeFileStamp stepplus_file_stamp(const std::string& path) {
    namespace fs = std::filesystem;
    const auto native_path = fs::u8path(path);
    std::error_code ec;
    if (!fs::is_regular_file(native_path, ec) || ec) return {};
    NativeFileStamp stamp;
    stamp.size = fs::file_size(native_path, ec);
    if (ec) return {};
    const fs::file_time_type modified = fs::last_write_time(native_path, ec);
    if (ec) return {};
    stamp.mtime_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
                         modified.time_since_epoch()).count();
    stamp.valid = true;
    return stamp;
}

BuildStateWriteback stepplus_write_build_state(const std::string& path,
                                               const NativeFileStamp& expected,
                                               const std::string& doc_hash,
                                               const std::map<std::string, std::string>& entries) {
    namespace fs = std::filesystem;
    BuildStateWriteback out;
    json manifest = cache_manifest(doc_hash);
    auto refuse = [&out](std::string why) {
        out.reason = std::move(why);
        return out;
    };
    auto is_build_state = [](const std::string& name) {
        return is_geometry_cache(name) || name == "cache.provenance" || name == "geometry.step";
    };
    if (!(stepplus_file_stamp(path) == expected)) return refuse("file_changed");

    const fs::path target = fs::u8path(path);
    NativeArchiveReader reader;
    if (!reader.open(target)) return refuse("not_a_container");
    auto& src = reader.zip;

    // The cache was built for this exact document; a file whose document.json no longer
    // hashes to it (an edit that kept size and mtime) keeps its own members.
    const int doc_index = mz_zip_reader_locate_file(&src, "document.json", nullptr, 0);
    if (doc_index < 0) return refuse("not_a_container");
    {
        std::size_t doc_size = 0;
        void* doc_heap = mz_zip_reader_extract_to_heap(&src, static_cast<mz_uint>(doc_index),
                                                       &doc_size, 0);
        if (!doc_heap) return refuse("io_error: cannot read document.json");
        std::string doc_bytes(static_cast<const char*>(doc_heap), doc_size);
        mz_free(doc_heap);
        try {
            const json authored = json::parse(doc_bytes);
            if (stepplus_doc_hash(authored) != doc_hash) return refuse("document_changed");
            manifest["doc_hash"] = util::sha256_hex(editable_document_json(authored));
        } catch (const std::exception&) {
            return refuse("not_a_container");
        }
    }

    // The reader admits the optional caches (cache.aicm, then cache.ckpt) only into the
    // expanded-byte budget every other member leaves — reserve exactly what stays.
    const mz_uint count = mz_zip_reader_get_num_files(&src);
    if (count > kMaxArchiveEntries) return refuse("io_error: archive entry count limit");
    mz_uint64 budget = kMaxArchiveExpandedBytes;
    auto reserve = [&budget](mz_uint64 bytes) { budget = bytes > budget ? 0 : budget - bytes; };
    std::vector<mz_uint> kept;
    kept.reserve(count);
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&src, i, &st)) return refuse("io_error: bad archive metadata");
        if (is_build_state(st.m_filename)) continue;
        reserve(st.m_uncomp_size);
        kept.push_back(i);
    }
    for (const auto& [name, data] : entries)
        if (!is_optional_cache(name)) reserve(data.size());
    reserve(2048); // bounded provenance envelope

    static std::atomic<uint64_t> serial{0};
    NativeStage stage;
    stage.directory = target.parent_path() /
        (".native-buildstate-" +
         std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) + "-" +
         std::to_string(serial.fetch_add(1)));
    const fs::path tmp = stage.directory / "container.tmp";
#if defined(_WIN32)
    auto& security = chudware_ipc::privateSecurity();
    if (!security.valid()) return refuse("io_error: cannot create private staging policy");
    if (!CreateDirectoryW(stage.directory.c_str(), &security.attributes))
        return refuse("io_error: cannot create private staging directory");
    stage.handle = CreateFileW(tmp.c_str(), GENERIC_WRITE | DELETE | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ, &security.attributes, CREATE_NEW, FILE_ATTRIBUTE_TEMPORARY |
        FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (stage.handle == INVALID_HANDLE_VALUE) return refuse("io_error: staging open failed");
    stage.descriptor = _open_osfhandle(reinterpret_cast<intptr_t>(stage.handle), _O_BINARY | _O_WRONLY);
    if (stage.descriptor < 0) return refuse("io_error: staging stream failed");
    stage.stream = _fdopen(stage.descriptor, "wb");
#else
    if (::mkdir(stage.directory.c_str(), 0700) != 0)
        return refuse("io_error: cannot create private staging directory");
    stage.directory_fd = ::open(stage.directory.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (stage.directory_fd < 0) return refuse("io_error: staging directory open failed");
    const int fd = ::openat(stage.directory_fd, "container.tmp",
        O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return refuse("io_error: staging open failed");
    stage.stream = ::fdopen(fd, "wb");
    if (!stage.stream) ::close(fd);
#endif
    if (!stage.stream) return refuse("io_error: staging stream failed");
    mz_zip_archive dst{};
    if (!mz_zip_writer_init_cfile(&dst, stage.stream, 0))
        return refuse("io_error: cannot create " + native_policy::path_utf8(tmp));
    bool dst_open = true;
    auto abandon = [&](std::string why) {
        if (dst_open) mz_zip_writer_end(&dst);
        dst_open = false;
        std::error_code ec;
        fs::remove(tmp, ec);
        out.entries.clear();
        out.dropped.clear();
        return refuse(std::move(why));
    };
    // Every authored member travels as its original compressed stream.
    for (const mz_uint i : kept)
        if (!mz_zip_writer_add_from_zip_reader(&dst, &src, i))
            return abandon("io_error: cannot copy an archive member");
    auto add = [&](const std::string& name, const std::string& data, bool stored) {
        const mz_uint level = stored ? (mz_uint)MZ_NO_COMPRESSION : (mz_uint)MZ_DEFAULT_LEVEL;
        return mz_zip_writer_add_mem(&dst, name.c_str(), data.data(), data.size(), level) != 0;
    };
    for (const auto& [name, data] : entries) {
        if (name == "cache.provenance") continue;
        if (!is_build_state(name)) continue;
        if (is_optional_cache(name)) {
            if (data.size() > budget) {
                out.dropped.push_back(name);
                continue;
            }
            budget -= data.size();
        }
        // Same storage rules as stepplus_save: optional caches and oversized geometry
        // caches STORED, the rest deflated.
        const bool stored = is_optional_cache(name) ||
                            (is_geometry_cache(name) && data.size() > kMaxEntryExpandedBytes);
        if (!add(name, data, stored)) return abandon("io_error: cannot add entry '" + name + "'");
        if (is_geometry_cache(name)) manifest["members"][name] = util::sha256_hex(data);
        out.entries.push_back(name);
    }
    if (!manifest["members"].empty()) {
        std::string provenance;
        try { provenance = sign_manifest(manifest); }
        catch (const std::exception& e) { return abandon(e.what()); }
        if (!add("cache.provenance", provenance, false)) return abandon("cache provenance write failed");
        out.entries.push_back("cache.provenance");
    }
    if (!mz_zip_writer_finalize_archive(&dst)) return abandon("io_error: cannot finalize archive");
    mz_zip_writer_end(&dst);
    dst_open = false;
    if (std::fflush(stage.stream) != 0) return abandon("io_error: staging flush failed");
    reader.close();

    // A save that landed while the copy was written wins; the build state is dropped.
    if (!(stepplus_file_stamp(path) == expected)) return abandon("file_changed");
#if defined(_WIN32)
    const auto destination = fs::absolute(target).make_preferred().native();
    const auto bytes = destination.size() * sizeof(wchar_t);
    // Include the terminator required by Win32 path resolution.
    std::vector<unsigned char> buffer(
        offsetof(FILE_RENAME_INFO, FileName) + bytes + sizeof(wchar_t));
    auto* rename = reinterpret_cast<FILE_RENAME_INFO*>(buffer.data());
    rename->ReplaceIfExists = TRUE;
    rename->FileNameLength = static_cast<DWORD>(bytes);
    std::memcpy(rename->FileName, destination.data(), bytes);
    if (!SetFileInformationByHandle(stage.handle, FileRenameInfo, rename,
                                   static_cast<DWORD>(buffer.size()))) {
        const DWORD error = GetLastError();
        return abandon("io_error: cannot publish build state (Windows error " +
                       std::to_string(error) + ")");
    }
    stage.published = true;
#else
    if (::renameat(stage.directory_fd, "container.tmp", AT_FDCWD, target.c_str()) != 0)
        return abandon("io_error: cannot publish build state");
#endif
    out.written = true;
    return out;
}

// --- studies/<id>/results.npz -------------------------------------------------
std::string stepplus_study_result_entry(const std::string& study_id) {
    return chudware::sim::cache::study_result_entry(study_id);
}

void stepplus_erase_study_results(std::map<std::string, std::string>& extra) {
    chudware::sim::cache::erase_study_results(extra);
}

bool stepplus_put_study_result(std::map<std::string, std::string>& extra,
                               const std::string& study_id, const std::string& kind,
                               const std::string& input_hash, const json& result) {
    using chudware::sim::cache::Kind;
    Kind k = Kind::FeaStatic;
    if (kind == "fea_static") k = Kind::FeaStatic;
    else if (kind == "fea_modal") k = Kind::FeaModal;
    else if (kind == "fea_thermal") k = Kind::FeaThermal;
    else if (kind == "spice") k = Kind::Spice;
    else if (kind == "cfd") k = Kind::Cfd;
    else if (kind == "em") k = Kind::Em;
    else return false;
    return chudware::sim::cache::put_study_result(extra, study_id, k, input_hash, result);
}

bool stepplus_get_study_result(const std::map<std::string, std::string>& extra,
                               const std::string& study_id, std::string& kind_out,
                               std::string& input_hash_out, json& result_out) {
    chudware::sim::cache::Kind k;
    if (!chudware::sim::cache::get_study_result(extra, study_id, k, input_hash_out, result_out))
        return false;
    kind_out = chudware::sim::cache::kind_name(k);
    return true;
}

}  // namespace chudware::doc
