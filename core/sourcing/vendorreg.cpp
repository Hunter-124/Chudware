// core/sourcing/vendorreg.cpp — vendor-parts registry write path. Port of
// engine_oracle/vendor_parts.py::register_local_cad (copy_file=True, which is what
// spec-sheet registration uses): copy the CAD under vendor_parts/<slug>/<slug>/,
// upsert the record in index.json (indent=2, sort_keys like the oracle), return the
// asdict-shaped VendorPart record. The Python-only PartsHub FTS rebuild side effect
// is NOT mirrored here (the hub is a reported remainder of T-RF18).
#include "sourcing/sourcing.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <sstream>

namespace fs = std::filesystem;

namespace chudware {
namespace sourcing {

namespace {

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
    if (ext == ".igs" || ext == ".iges") return "iges";
    if (ext == ".brep") return "brep";
    if (ext == ".stl") return "stl";
    throw ProviderError("unsupported CAD extension '" + p.extension().string() + "'");
}

json load_index(const fs::path& index) {
    std::error_code ec;
    if (!fs::exists(index, ec)) return json::object();
    try {
        std::ifstream in(index);
        std::stringstream ss;
        ss << in.rdbuf();
        json data = json::parse(ss.str());
        return data.is_object() ? data : json::object();
    } catch (...) {
        return json::object();
    }
}

}  // namespace

json register_local_cad(const std::string& supplier, const std::string& part_number,
                        const std::string& cad_path, const std::string& description,
                        const std::string& product_url, const json& specs,
                        const std::string& source) {
    if (part_number.empty()) throw ProviderError("part_number is required");
    std::error_code ec;
    fs::path src = fs::absolute(fs::path(cad_path), ec).lexically_normal();
    if (ec || !fs::exists(src, ec))
        throw ProviderError("CAD file not found: " + src.string());

    json rec;
    rec["supplier"] = supplier;
    rec["part_number"] = part_number;
    rec["description"] = description;
    rec["cad_path"] = std::string();  // filled after the copy
    rec["cad_format"] = detect_format(src);
    rec["product_url"] = product_url;
    rec["specs"] = specs.is_object() ? specs : json::object();
    rec["source"] = source;
    rec["added_ts"] = now_epoch();
    rec["source_mtime"] = file_mtime_epoch(src);
    rec["unit_price"] = nullptr;
    rec["currency"] = "USD";
    rec["price_breaks"] = json::array();
    rec["stock"] = nullptr;
    rec["lead_time_days"] = nullptr;
    rec["moq"] = nullptr;
    rec["fetched_ts"] = nullptr;

    const fs::path root = vendor_parts_dir();
    const fs::path dest_dir = root / vendor_slug(supplier) / vendor_slug(part_number);
    fs::create_directories(dest_dir, ec);
    const fs::path dest = dest_dir / src.filename();
    fs::copy_file(src, dest, fs::copy_options::overwrite_existing, ec);
    if (ec) throw ProviderError("cannot copy CAD file: " + ec.message());
    rec["cad_path"] = dest.string();

    const fs::path index = root / "index.json";
    json idx = load_index(index);
    idx[vendor_slug(supplier) + "::" + vendor_slug(part_number)] = rec;
    fs::create_directories(root, ec);
    std::ofstream out(index);
    if (!out) throw ProviderError("cannot write vendor index: " + index.string());
    out << idx.dump(2);  // object keys sort lexicographically, matching sort_keys=True
    return rec;
}

}  // namespace sourcing
}  // namespace chudware
