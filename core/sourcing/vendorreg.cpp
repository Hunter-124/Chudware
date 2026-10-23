// core/sourcing/vendorreg.cpp — vendor-parts registry write path. Port of
// engine_oracle/vendor_parts.py::register_local_cad (copy_file=True, which is what
// spec-sheet registration uses): copy the CAD under vendor_parts/<slug>/<slug>/,
// upsert the record in index.json (indent=2, sort_keys like the oracle), return the
// asdict-shaped VendorPart record. The Python-only PartsHub FTS rebuild side effect
// is NOT mirrored here.
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
    json idx = load_index(index);
    const std::string key = vendor_slug(supplier) + "::" + vendor_slug(part_number);
    const bool creating = !idx.contains(key) || !idx[key].is_object();
    json rec = creating ? json::object() : idx[key];
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
    std::error_code ec;
    fs::create_directories(root, ec);
    std::ofstream out(index);
    if (!out) throw ProviderError("cannot write vendor index: " + index.string());
    out << idx.dump(2);  // object keys sort lexicographically, matching sort_keys=True
    return rec;
}

}  // namespace sourcing
}  // namespace chudware
