// core/sourcing/vendorreg.cpp — vendor-parts registry write path. Port of
// engine_oracle/vendor_parts.py::register_local_cad (copy_file=True, which is what
// spec-sheet registration uses): copy the CAD under vendor_parts/<slug>/<slug>/,
// upsert the record in index.json (indent=2, sort_keys like the oracle), return the
// asdict-shaped VendorPart record. The Python-only PartsHub FTS rebuild side effect
// is NOT mirrored here.
#include "sourcing/sourcing.h"

#include <algorithm>
#include <cstdlib>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <mutex>
#include <vector>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

namespace chudware {
namespace sourcing {

namespace {
// Shared with services.parts.vendor_parts: index.json.lock, byte 0 on Windows.
class RegistryLock {
public:
    explicit RegistryLock(const fs::path& index) : guard_(mutex_) {
        fs::create_directories(index.parent_path());
        fs::path path = index;
        path += ".lock";
#ifdef _WIN32
        handle_ = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle_ == INVALID_HANDLE_VALUE) throw ProviderError("cannot open vendor registry lock");
        OVERLAPPED position{};
        if (!LockFileEx(handle_, LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, &position)) {
            CloseHandle(handle_);
            handle_ = INVALID_HANDLE_VALUE;
            throw ProviderError("cannot lock vendor registry");
        }
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(handle_, &size)) {
            UnlockFileEx(handle_, 0, 1, 0, &position);
            CloseHandle(handle_);
            handle_ = INVALID_HANDLE_VALUE;
            throw ProviderError("cannot inspect vendor registry lock");
        }
        if (size.QuadPart == 0) {
            DWORD written = 0;
            const char byte = 0;
            if (!WriteFile(handle_, &byte, 1, &written, nullptr) || written != 1) {
                UnlockFileEx(handle_, 0, 1, 0, &position);
                CloseHandle(handle_);
                handle_ = INVALID_HANDLE_VALUE;
                throw ProviderError("cannot initialize vendor registry lock");
            }
        }
#else
        fd_ = ::open(path.c_str(), O_CREAT | O_RDWR, 0600);
        if (fd_ < 0) throw ProviderError("cannot open vendor registry lock");
        if (::flock(fd_, LOCK_EX) != 0) {
            ::close(fd_);
            fd_ = -1;
            throw ProviderError("cannot lock vendor registry");
        }
#endif
    }
    ~RegistryLock() {
#ifdef _WIN32
        if (handle_ != INVALID_HANDLE_VALUE) {
            OVERLAPPED position{};
            UnlockFileEx(handle_, 0, 1, 0, &position);
            CloseHandle(handle_);
        }
#else
        if (fd_ >= 0) { ::flock(fd_, LOCK_UN); ::close(fd_); }
#endif
    }
    RegistryLock(const RegistryLock&) = delete;
    RegistryLock& operator=(const RegistryLock&) = delete;
private:
    static std::mutex mutex_;
    std::lock_guard<std::mutex> guard_;
#ifdef _WIN32
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int fd_ = -1;
#endif
};
std::mutex RegistryLock::mutex_;

void write_index_atomic(const fs::path& index, const std::string& payload) {
#ifdef _WIN32
    wchar_t temp[MAX_PATH];
    if (!GetTempFileNameW(index.parent_path().c_str(), L"vnd", 0, temp))
        throw ProviderError("cannot create vendor registry staging file");
    HANDLE file = CreateFileW(temp, GENERIC_WRITE, 0, nullptr, TRUNCATE_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    bool ok = file != INVALID_HANDLE_VALUE;
    size_t offset = 0;
    while (ok && offset < payload.size()) {
        DWORD n = 0;
        DWORD amount = static_cast<DWORD>((std::min)(payload.size() - offset, size_t(1 << 20)));
        ok = WriteFile(file, payload.data() + offset, amount, &n, nullptr) && n;
        offset += n;
    }
    if (ok) ok = FlushFileBuffers(file);
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    if (ok) ok = MoveFileExW(temp, index.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH);
    if (!ok) { DeleteFileW(temp); throw ProviderError("cannot write vendor registry"); }
#else
    std::string pattern = (index.parent_path() / ".vendor-index-XXXXXX").string();
    std::vector<char> name(pattern.begin(), pattern.end());
    name.push_back('\0');
    int fd = ::mkstemp(name.data());
    if (fd < 0) throw ProviderError("cannot create vendor registry staging file");
    size_t offset = 0;
    bool ok = true;
    while (offset < payload.size()) {
        auto n = ::write(fd, payload.data() + offset, payload.size() - offset);
        if (n <= 0) { ok = false; break; }
        offset += static_cast<size_t>(n);
    }
    if (ok) ok = ::fsync(fd) == 0;
    if (::close(fd) != 0) ok = false;
    if (ok) ok = ::rename(name.data(), index.c_str()) == 0;
    if (!ok) { ::unlink(name.data()); throw ProviderError("cannot write vendor registry"); }
    int dir = ::open(index.parent_path().c_str(), O_RDONLY);
    if (dir >= 0) { ::fsync(dir); ::close(dir); }
#endif
}


double file_mtime_epoch(const fs::path& p) {
    std::error_code ec;
    const auto ft = fs::last_write_time(p, ec);
    if (ec) return 0.0;
    // file_clock -> system_clock (pre-C++20 portable form).
    const auto now_f = decltype(ft)::clock::now();
    const auto now_s = std::chrono::system_clock::now();
    const auto st = std::chrono::time_point_cast<std::chrono::system_clock::duration>(
        ft - now_f + now_s);
    return std::chrono::duration<double>(st.time_since_epoch()).count();
}

double now_epoch() {
    return std::chrono::duration<double>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string detect_format(const fs::path& p) {
    std::string ext = p.extension().string();
    for (char& c : ext) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (ext == ".step" || ext == ".stp") return "step";
    if (ext == ".step+") return "step+";
    if (ext == ".igs" || ext == ".iges") return "iges";
    if (ext == ".brep") return "brep";
    if (ext == ".stl") return "stl";
    throw ProviderError("unsupported CAD extension '" + p.extension().string() + "'");
}

json load_index(const fs::path& index) {
    std::error_code ec;
    if (!fs::exists(index, ec)) return json::object();
    std::ifstream in(index);
    if (!in) throw ProviderError("cannot read vendor registry");
    try {
        json data = json::parse(in);
        if (!data.is_object()) throw ProviderError("vendor registry must be a JSON object");
        return data;
    } catch (const json::exception&) {
        throw ProviderError("invalid vendor registry JSON");
    }
}

}  // namespace

json vendor_index_lookup(const json& index, const json& component) {
    if (!index.is_object() || !component.is_object()) return json();

    std::string part_number = component.value("mpn", std::string());
    if (part_number.empty())
        part_number = component.value("part_number", std::string());
    if (part_number.empty()) return json();

    std::string supplier = component.value("supplier", std::string());
    if (supplier.empty())
        supplier = component.value("vendor_supplier", std::string());

    // Registry keys are always slug(supplier)::slug(part_number). Prefer that
    // exact supplier-qualified match before the deterministic key-order scan.
    if (!supplier.empty()) {
        const auto exact =
            index.find(vendor_slug(supplier) + "::" + vendor_slug(part_number));
        if (exact != index.end()) return *exact;
    }

    const std::string part_slug = vendor_slug(part_number);
    for (auto it = index.begin(); it != index.end(); ++it) {
        if (!it->is_object()) continue;
        const json candidate = it->value("part_number", json());
        if (candidate.is_string() &&
            vendor_slug(candidate.get<std::string>()) == part_slug)
            return *it;
    }
    return json();
}

json register_local_cad(const std::string& supplier, const std::string& part_number,
                        const json& fields) {
    if (supplier.empty()) throw ProviderError("supplier is required");
    if (part_number.empty()) throw ProviderError("part_number is required");
    if (!fields.is_object()) throw ProviderError("fields must be an object");

    if (fields.contains("price_breaks")) {
        const json& breaks = fields["price_breaks"];
        if (!breaks.is_array())
            throw ProviderError("price_breaks must be an array; got " + breaks.dump());
        for (size_t i = 0; i < breaks.size(); ++i) {
            const json& entry = breaks[i];
            if (!entry.is_object() || !entry.contains("qty") ||
                !entry["qty"].is_number() || !entry.contains("unit_price") ||
                !entry["unit_price"].is_number()) {
                throw ProviderError("price_breaks[" + std::to_string(i) +
                                    "] must be an object with numeric qty and unit_price; got " +
                                    entry.dump());
            }
        }
    }

    const fs::path root = vendor_parts_dir();
    const fs::path index = root / "index.json";
    RegistryLock lock(index);
    json idx = load_index(index);
    const std::string key = vendor_slug(supplier) + "::" + vendor_slug(part_number);
    const bool creating = !idx.contains(key) || !idx[key].is_object();
    json rec = creating ? json::object() : idx[key];
    if (!creating && (rec.value("supplier", std::string()) != supplier ||
                      rec.value("part_number", std::string()) != part_number))
        throw ProviderError("vendor registry key collision: " + key);
    const double write_ts = now_epoch();

    rec["supplier"] = supplier;
    rec["part_number"] = part_number;
    if (!rec.contains("description")) rec["description"] = std::string();
    if (!rec.contains("cad_path")) rec["cad_path"] = std::string();
    if (!rec.contains("cad_format")) rec["cad_format"] = nullptr;
    if (!rec.contains("product_url")) rec["product_url"] = std::string();
    if (!rec.contains("specs")) rec["specs"] = json::object();
    if (!rec.contains("source")) rec["source"] = std::string();
    if (!rec.contains("added_ts")) rec["added_ts"] = write_ts;
    if (!rec.contains("source_mtime")) rec["source_mtime"] = nullptr;
    if (!rec.contains("unit_price")) rec["unit_price"] = nullptr;
    if (!rec.contains("currency")) rec["currency"] = "USD";
    if (!rec.contains("price_breaks")) rec["price_breaks"] = json::array();
    if (!rec.contains("stock")) rec["stock"] = nullptr;
    if (!rec.contains("lead_time_days")) rec["lead_time_days"] = nullptr;
    if (!rec.contains("moq")) rec["moq"] = nullptr;
    if (!rec.contains("fetched_ts")) rec["fetched_ts"] = nullptr;
    if (!rec.contains("provenance")) rec["provenance"] = json::object();

    for (const char* field : {"description", "product_url", "specs", "source",
                              "unit_price", "currency", "price_breaks", "stock",
                              "lead_time_days", "moq", "provenance"}) {
        if (fields.contains(field)) rec[field] = fields[field];
    }

    const bool supplies_price =
        fields.contains("unit_price") || fields.contains("price_breaks") ||
        fields.contains("stock") || fields.contains("lead_time_days") ||
        fields.contains("moq");
    if (fields.contains("fetched_ts"))
        rec["fetched_ts"] = fields["fetched_ts"];
    else if (supplies_price)
        rec["fetched_ts"] = write_ts;

    std::string cad_path;
    if (fields.contains("cad_path") && !fields["cad_path"].is_null()) {
        if (!fields["cad_path"].is_string())
            throw ProviderError("cad_path must be a string");
        cad_path = fields["cad_path"].get<std::string>();
    }
    if (!cad_path.empty()) {
        std::error_code ec;
        fs::path src = fs::absolute(fs::path(cad_path), ec).lexically_normal();
        if (ec || !fs::exists(src, ec))
            throw ProviderError("CAD file not found: " + src.string());
        const std::string cad_format = detect_format(src);

        const fs::path dest_dir = root / vendor_slug(supplier) / vendor_slug(part_number);
        fs::create_directories(dest_dir, ec);
        const fs::path dest = dest_dir / src.filename();
        fs::copy_file(src, dest, fs::copy_options::overwrite_existing, ec);
        if (ec) throw ProviderError("cannot copy CAD file: " + ec.message());
        rec["cad_path"] = dest.string();
        rec["cad_format"] = cad_format;
        rec["source_mtime"] = file_mtime_epoch(src);
    }

    rec["updated_ts"] = write_ts;
    idx[key] = rec;
    write_index_atomic(index, idx.dump(2));
    return rec;
}

}  // namespace sourcing
}  // namespace chudware
