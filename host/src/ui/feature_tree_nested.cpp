#include "feature_tree_nested.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <map>
#include <system_error>

#include "miniz.h"

namespace ui {
namespace {

namespace fs = std::filesystem;

struct CacheSlot {
    fs::file_time_type mtime{};
    std::uintmax_t size = 0;
    bool statOk = false;
    NestedDocument doc;
};

std::map<std::string, CacheSlot>& cache() {
    static std::map<std::string, CacheSlot> slots;
    return slots;
}

std::string normalizePath(const std::string& path) {
    if (path.empty()) return {};
    std::error_code ec;
    fs::path p = fs::weakly_canonical(fs::path(path), ec);
    if (ec) p = fs::path(path).lexically_normal();
    return p.string();
}

bool readZipEntry(const std::string& zipPath, const char* entry, std::string& out) {
    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_file(&zip, zipPath.c_str(), 0)) return false;
    bool ok = false;
    const int index = mz_zip_reader_locate_file(&zip, entry, nullptr, 0);
    if (index >= 0) {
        size_t size = 0;
        void* data = mz_zip_reader_extract_to_heap(&zip, static_cast<mz_uint>(index), &size, 0);
        if (data) {
            out.assign(static_cast<const char*>(data), size);
            mz_free(data);
            ok = true;
        }
    }
    mz_zip_reader_end(&zip);
    return ok;
}

std::string jsonString(const nlohmann::json& object, const char* key) {
    const auto it = object.find(key);
    return it != object.end() && it->is_string() ? it->get<std::string>() : std::string();
}

NestedDocument parseDocument(const std::string& absPath) {
    NestedDocument result;
    result.path = absPath;
    result.fileName = fs::path(absPath).filename().string();

    std::string documentJson;
    if (!readZipEntry(absPath, "document.json", documentJson)) {
        std::string ext = fs::path(absPath).extension().string();
        std::transform(ext.begin(), ext.end(), ext.begin(),
                       [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
        if (ext == ".step+" || ext == ".asmstep") {
            result.state = NestedDocumentState::Unreadable;
            result.error = "Native component has no readable document.json";
        } else {
            result.state = NestedDocumentState::External;
            result.error = "Imported geometry has no editable feature history";
        }
        return result;
    }

    nlohmann::json tree;
    try {
        tree = nlohmann::json::parse(documentJson);
    } catch (const std::exception& e) {
        result.state = NestedDocumentState::Unreadable;
        result.error = std::string("Invalid component document: ") + e.what();
        return result;
    }
    if (!tree.is_object()) {
        result.state = NestedDocumentState::Unreadable;
        result.error = "Component document root is not an object";
        return result;
    }

    result.state = NestedDocumentState::Ready;
    result.name = jsonString(tree, "name");
    result.schema = jsonString(tree, "schema");
    const auto material = tree.find("material");
    if (material != tree.end()) {
        if (material->is_object()) result.materialKey = jsonString(*material, "key");
        else if (material->is_string()) result.materialKey = material->get<std::string>();
    }

    const auto features = tree.find("features");
    if (features != tree.end() && features->is_array()) {
        result.features.reserve(features->size());
        for (const auto& feature : *features) {
            if (!feature.is_object()) continue;
            NestedFeature nested;
            nested.id = jsonString(feature, "id");
            nested.name = jsonString(feature, "name");
            nested.type = jsonString(feature, "type");
            nested.folder = jsonString(feature, "folder");
            nested.raw = feature;
            if (nested.name.empty())
                nested.name = nested.id.empty() ? std::string("Feature") : nested.id;
            result.features.push_back(std::move(nested));
        }
    }

    const auto components = tree.find("components");
    if (components != tree.end() && components->is_array()) {
        result.components.reserve(components->size());
        const fs::path base = fs::path(absPath).parent_path();
        for (const auto& component : *components) {
            if (!component.is_object()) continue;
            NestedComponent nested;
            nested.id = jsonString(component, "id");
            nested.name = jsonString(component, "name");
            if (nested.name.empty()) nested.name = nested.id.empty() ? "Component" : nested.id;
            nested.fixed = component.value("fixed", false);
            nested.suppressed = component.value("suppressed", false);
            nested.hidden = component.value("hidden", false);
            const auto source = component.find("source");
            if (source != component.end() && source->is_object())
                nested.sourcePath = jsonString(*source, "path");
            if (!nested.sourcePath.empty()) {
                fs::path sourcePath(nested.sourcePath);
                if (!sourcePath.is_absolute()) sourcePath = base / sourcePath;
                nested.sourceAbsPath = normalizePath(sourcePath.string());
            }
            result.components.push_back(std::move(nested));
        }
    }
    return result;
}

}  // namespace

const NestedDocument& featureTreeNestedDocument(const std::string& inputPath) {
    static const NestedDocument empty;
    const std::string absPath = normalizePath(inputPath);
    if (absPath.empty()) return empty;

    std::error_code ec;
    const fs::file_time_type mtime = fs::last_write_time(absPath, ec);
    const bool statOk = !ec;
    std::uintmax_t size = 0;
    if (statOk) {
        size = fs::file_size(absPath, ec);
        if (ec) size = 0;
    }

    auto& slots = cache();
    auto it = slots.find(absPath);
    if (it != slots.end() && it->second.statOk == statOk &&
        (!statOk || (it->second.mtime == mtime && it->second.size == size)))
        return it->second.doc;

    CacheSlot slot;
    slot.statOk = statOk;
    slot.mtime = mtime;
    slot.size = size;
    if (!statOk) {
        slot.doc.state = NestedDocumentState::Missing;
        slot.doc.path = absPath;
        slot.doc.fileName = fs::path(absPath).filename().string();
        slot.doc.error = "Component file is missing or unavailable";
    } else {
        slot.doc = parseDocument(absPath);
    }
    return slots.insert_or_assign(absPath, std::move(slot)).first->second.doc;
}

}  // namespace ui
