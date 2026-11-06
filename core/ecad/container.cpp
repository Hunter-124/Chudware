// Read-only legacy .ecad+ container migration — see container.h.

#include "ecad/container.h"

#include <cstdio>
#include <filesystem>
#include <string>

#include <miniz.h>

#include "ecad/ops.h"  // OpError

namespace chudware::ecad {

using nlohmann::json;

namespace {


// All retained data is budgeted, including forward-compatible unknown entries.
constexpr mz_uint kMaxEntries = 4096;
constexpr mz_uint64 kMaxEntryBytes = 64 * 1024 * 1024;
constexpr mz_uint64 kMaxInflatedBytes = 256 * 1024 * 1024;

struct ZipReaderGuard {
    mz_zip_archive* zip;
    std::FILE* file;
    ~ZipReaderGuard() { mz_zip_reader_end(zip); std::fclose(file); }
};
// Known entries are not retained as unknown attachments.
bool is_known_entry(const std::string& name) {
    return name == "document.json" || name == "meta.json" || name == "netlist.json" ||
           name == "thumbnail.png";
}


[[noreturn]] void io_error(const std::string& msg) {
    throw OpError("io_error", msg, json{{"reason", msg}});
}

}  // namespace


EcadContainerDoc ecad_container_open(const std::string& path) {
    const auto native_path = std::filesystem::u8path(path);
    if (!std::filesystem::exists(native_path)) io_error("ecad_open: no such file: " + path);

    mz_zip_archive zip{};
#ifdef _WIN32
    std::FILE* file = _wfopen(native_path.c_str(), L"rb");
#else
    std::FILE* file = std::fopen(native_path.c_str(), "rb");
#endif
    if (!file) io_error("ecad_open: cannot read " + path);
    if (!mz_zip_reader_init_cfile(&zip, file, 0, 0)) {
        std::fclose(file);
        io_error("ecad_open: not a valid .ecad+ (zip) container: " + path);
    }

    ZipReaderGuard guard{&zip, file};
    const mz_uint count = mz_zip_reader_get_num_files(&zip);
    if (count > kMaxEntries) io_error("ecad_open: ZIP entry count limit exceeded");
    mz_uint64 declared_total = 0;
    std::map<std::string, mz_uint> entries;
    for (mz_uint i = 0; i < count; ++i) {
        mz_zip_archive_file_stat st{};
        if (!mz_zip_reader_file_stat(&zip, i, &st))
            io_error("ecad_open: invalid ZIP member metadata");
        if (st.m_uncomp_size > kMaxEntryBytes)
            io_error("ecad_open: ZIP entry byte limit exceeded");
        if (st.m_uncomp_size > kMaxInflatedBytes - declared_total)
            io_error("ecad_open: ZIP aggregate byte limit exceeded");
        declared_total += st.m_uncomp_size;
        // file_stat's display filename is truncated; preservation uses the full
        // member name returned by the reader, even for long unknown attachments.
        const mz_uint name_size = mz_zip_reader_get_filename(&zip, i, nullptr, 0);
        if (!name_size) io_error("ecad_open: invalid ZIP member name");
        std::string name(name_size, '\0');
        if (mz_zip_reader_get_filename(&zip, i, name.data(), name_size) != name_size)
            io_error("ecad_open: invalid ZIP member name");
        name.resize(name_size - 1);
        // Read indexed members, not repeated locate-by-name aliases.
        if (!entries.emplace(std::move(name), i).second)
            io_error("ecad_open: duplicate ZIP member name");
    }
    mz_uint64 inflated_total = 0;
    auto read_entry = [&](const std::string& name, std::string& out) -> bool {
        const auto entry = entries.find(name);
        if (entry == entries.end()) return false;
        mz_zip_archive_file_stat st{};
        if (!mz_zip_reader_file_stat(&zip, entry->second, &st))
            io_error("ecad_open: invalid ZIP member metadata");
        if (st.m_uncomp_size > kMaxEntryBytes ||
            st.m_uncomp_size > kMaxInflatedBytes - inflated_total)
            io_error("ecad_open: ZIP inflation byte limit exceeded");
        // miniz refuses output beyond this fixed buffer (including dishonest
        // compressed streams); no extract_to_heap allocation precedes the check.
        out.resize(static_cast<std::size_t>(st.m_uncomp_size));
        if (!mz_zip_reader_extract_to_mem(&zip, entry->second, out.data(), out.size(), 0))
            io_error("ecad_open: cannot inflate ZIP entry '" + name + "'");
        inflated_total += out.size();
        return true;
    };

    EcadContainerDoc result;

    std::string meta_bytes;
    if (!read_entry("meta.json", meta_bytes)) {
        io_error("not a .ecad+ container: meta.json missing");
    }
    json meta;
    try {
        meta = json::parse(meta_bytes);
    } catch (const std::exception& e) {
        io_error(std::string("ecad_open: bad meta.json: ") + e.what());
    }
    const std::string container = meta.value("container", std::string());
    if (container.rfind("aicad.ecad+/", 0) != 0) {
        io_error("unknown container '" + container + "'");
    }
    const std::string major =
        (container.find('/') != std::string::npos) ? container.substr(container.rfind('/') + 1) : "";
    if (!major.empty() && major != "1") {
        io_error("unsupported .ecad+ major version '" + major + "'");
    }

    std::string doc_bytes;
    if (!read_entry("document.json", doc_bytes)) {
        io_error("not a .ecad+ container: document.json missing");
    }
    try {
        result.doc = json::parse(doc_bytes);
    } catch (const std::exception& e) {
        io_error(std::string("ecad_open: bad document.json: ") + e.what());
    }

    // Read unknown entries for diagnostics and legacy recovery metadata.
    for (const auto& [name, index] : entries) {
        (void)index;
        if (is_known_entry(name) && name != "thumbnail.png") continue;
        std::string data;
        if (read_entry(name, data)) result.extra[name] = std::move(data);
    }

    return result;
}

}  // namespace chudware::ecad
