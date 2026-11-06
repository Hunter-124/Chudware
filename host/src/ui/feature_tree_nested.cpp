#include "feature_tree_nested.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <filesystem>
#include <map>
#include <mutex>
#include <thread>
#include <system_error>

#include "miniz.h"
#include "anim.h"
#include "../core/doc_mirror.h"

namespace ui {
namespace {

namespace fs = std::filesystem;

using Clock = std::chrono::steady_clock;

struct CanonicalPaths {
    std::mutex mutex;
    std::map<std::string, std::string> paths;
};

CanonicalPaths& canonicalPaths() {
    static CanonicalPaths paths;
    return paths;
}

// Preserve the nested history's existing profile/sketch selectors while deriving them off-thread.
void scanNestedSketchRefs(const nlohmann::json& node, const std::string& key,
                          const std::unordered_set<std::string>& sketchIds,
                          std::unordered_set<std::string>& seen) {
    if (node.is_object()) {
        const auto kind = node.find("kind"), sketch = node.find("sketch");
        if (kind != node.end() && kind->is_string() &&
            kind->get_ref<const std::string&>() == "region" &&
            sketch != node.end() && sketch->is_string() &&
            sketchIds.count(sketch->get_ref<const std::string&>()))
            seen.insert(sketch->get_ref<const std::string&>());
        for (auto it = node.begin(); it != node.end(); ++it)
            scanNestedSketchRefs(it.value(), it.key(), sketchIds, seen);
    } else if (node.is_array()) {
        for (const auto& item : node) scanNestedSketchRefs(item, key, sketchIds, seen);
    } else if (node.is_string() && (key == "profile" || key.find("sketch") != std::string::npos)) {
        const auto& value = node.get_ref<const std::string&>();
        if (sketchIds.count(value)) seen.insert(value);
    }
}

void deriveFeatures(NestedDocument& document) {
    std::unordered_set<std::string> sketchIds;
    for (size_t i = 0; i < document.features.size(); ++i) {
        const auto& feature = document.features[i];
        document.featureById.emplace(feature.id, i);
        if (!feature.folder.empty())
            document.folderIds.try_emplace(feature.folder, "##cfld_" + feature.folder);
        if (feature.type == "sketch") sketchIds.insert(feature.id);
        if (feature.type == "plane" || feature.type == "datum_plane" ||
            feature.type == "axis" || feature.type == "point" || feature.folder == "Reference")
            document.references.push_back(i);
    }
    std::unordered_map<std::string, int> counts;
    std::unordered_map<std::string, std::string> consumer;
    for (const auto& feature : document.features) {
        if (feature.type == "sketch") continue;
        const auto refs = feature.raw.find("refs");
        if (refs == feature.raw.end()) continue;
        std::unordered_set<std::string> seen;
        scanNestedSketchRefs(*refs, {}, sketchIds, seen);
        for (const auto& sketch : seen) {
            ++counts[sketch];
            consumer[sketch] = feature.id;
        }
    }
    for (const auto& item : consumer) {
        if (counts[item.first] != 1) continue;
        auto feature = document.featureById.find(item.first);
        if (feature == document.featureById.end()) continue;
        document.childrenOf[item.second].push_back(feature->second);
        document.nestedSketches.insert(item.first);
    }
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

NestedDocument parseTree(const std::string& absPath, const nlohmann::json& tree) {
    NestedDocument result;
    result.path = absPath;
    result.fileName = fs::path(absPath).filename().string();
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
            nested.popupId = "##nested_feature_" + nested.id;
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
                nested.sourceAbsPath = featureTreeCanonicalPath(sourcePath.string());
            }
            result.components.push_back(std::move(nested));
        }
    }
    deriveFeatures(result);
    return result;
}

NestedDocument parseDocument(const std::string& absPath) {
    std::string documentJson;
    if (!readZipEntry(absPath, "document.json", documentJson)) {
        NestedDocument result;
        result.path = absPath;
        result.fileName = fs::path(absPath).filename().string();
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
    try {
        return parseTree(absPath, nlohmann::json::parse(documentJson));
    } catch (const std::exception& e) {
        NestedDocument result;
        result.path = absPath;
        result.fileName = fs::path(absPath).filename().string();
        result.state = NestedDocumentState::Unreadable;
        result.error = std::string("Invalid component document: ") + e.what();
        return result;
    }
}

struct CacheSlot {
    std::string path;
    fs::file_time_type mtime{};
    std::uintmax_t size = 0;
    bool statOk = false;
    bool checked = false;
    bool pending = false;
    uint64_t version = 0;
    uint64_t used = 0;
    Clock::time_point nextCheck{};
    std::shared_ptr<const NestedDocument> doc;
};

class DocumentCache {
public:
    DocumentCache() {
        // The canonical memo must outlive the worker during static teardown.
        (void)canonicalPaths();
        worker_ = std::thread([this] { run(); });
    }
    ~DocumentCache() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        ready_.notify_one();
        worker_.join();
    }

    std::shared_ptr<const NestedDocument> get(const std::string& path) {
        std::lock_guard<std::mutex> lock(mutex_);
        auto it = slots_.find(path);
        if (it == slots_.end()) {
            if (slots_.size() >= 1024) {
                auto oldest = slots_.end();
                for (auto candidate = slots_.begin(); candidate != slots_.end(); ++candidate)
                    if (!candidate->second->pending &&
                        (oldest == slots_.end() || candidate->second->used < oldest->second->used))
                        oldest = candidate;
                if (oldest != slots_.end()) slots_.erase(oldest);
                else return loadingDocument();
            }
            auto slot = std::make_shared<CacheSlot>();
            slot->path = path;
            auto document = std::make_shared<NestedDocument>();
            document->state = NestedDocumentState::Loading;
            document->path = path;
            document->fileName = fs::path(path).filename().string();
            document->error = "Loading feature history...";
            slot->doc = std::move(document);
            it = slots_.emplace(path, std::move(slot)).first;
        }
        auto& slot = *it->second;
        slot.used = ++used_;
        if (!slot.pending && Clock::now() >= slot.nextCheck) {
            slot.pending = true;
            jobs_.push_back(it->second);
            ready_.notify_one();
        }
        return slot.doc;
    }

    void invalidate(const std::string& path) {
        std::lock_guard<std::mutex> lock(mutex_);
        for (auto& item : slots_) {
            if (!path.empty() && item.first != path) continue;
            ++item.second->version;
            item.second->checked = false;
            item.second->nextCheck = {};
        }
    }

private:
    static std::shared_ptr<const NestedDocument> loadingDocument() {
        static const auto document = [] {
            auto value = std::make_shared<NestedDocument>();
            value->state = NestedDocumentState::Loading;
            value->error = "Loading feature history...";
            return value;
        }();
        return document;
    }

    void run() {
        for (;;) {
            std::shared_ptr<CacheSlot> slot;
            uint64_t version;
            bool checked, wasStatOk;
            fs::file_time_type oldMtime;
            std::uintmax_t oldSize;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [this] { return stopping_ || !jobs_.empty(); });
                if (stopping_) return;
                slot = std::move(jobs_.front());
                jobs_.pop_front();
                version = slot->version;
                checked = slot->checked;
                wasStatOk = slot->statOk;
                oldMtime = slot->mtime;
                oldSize = slot->size;
            }
            std::error_code ec;
            const auto mtime = fs::last_write_time(slot->path, ec);
            const bool statOk = !ec;
            std::uintmax_t size = statOk ? fs::file_size(slot->path, ec) : 0;
            if (ec) size = 0;
            std::shared_ptr<const NestedDocument> document;
            if (!checked || statOk != wasStatOk ||
                (statOk && (mtime != oldMtime || size != oldSize))) {
                try {
                    if (statOk) {
                        document = std::make_shared<NestedDocument>(parseDocument(slot->path));
                    } else {
                        auto missing = std::make_shared<NestedDocument>();
                        missing->path = slot->path;
                        missing->fileName = fs::path(slot->path).filename().string();
                        missing->error = "Component file is missing or unavailable";
                        document = std::move(missing);
                    }
                } catch (const std::exception& error) {
                    auto unreadable = std::make_shared<NestedDocument>();
                    unreadable->state = NestedDocumentState::Unreadable;
                    unreadable->path = slot->path;
                    unreadable->error = error.what();
                    document = std::move(unreadable);
                }
            }
            {
                std::lock_guard<std::mutex> lock(mutex_);
                slot->pending = false;
                if (slot->version == version) {
                    slot->checked = true;
                    slot->statOk = statOk;
                    slot->mtime = mtime;
                    slot->size = size;
                    slot->nextCheck = Clock::now() + std::chrono::seconds(1);
                    if (document) slot->doc = std::move(document);
                }
            }
            ui::anim::wakeLoop();
        }
    }

    std::mutex mutex_;
    std::condition_variable ready_;
    std::map<std::string, std::shared_ptr<CacheSlot>> slots_;
    std::deque<std::shared_ptr<CacheSlot>> jobs_;
    uint64_t used_ = 0;
    bool stopping_ = false;
    std::thread worker_;
};

DocumentCache& cache() {
    static DocumentCache documents;
    return documents;
}

}  // namespace

std::string featureTreeCanonicalPath(const std::string& path) {
    if (path.empty()) return {};
    auto& memo = canonicalPaths();
    {
        std::lock_guard<std::mutex> lock(memo.mutex);
        const auto it = memo.paths.find(path);
        if (it != memo.paths.end()) return it->second;
    }
    std::error_code ec;
    fs::path normalized = fs::weakly_canonical(fs::path(path), ec);
    if (ec) normalized = fs::path(path).lexically_normal();
    std::lock_guard<std::mutex> lock(memo.mutex);
    if (memo.paths.size() >= 4096) memo.paths.clear();
    const std::string canonical = normalized.string();
    memo.paths.try_emplace(canonical, canonical);
    return memo.paths.try_emplace(path, canonical).first->second;
}

std::shared_ptr<const NestedDocument> featureTreeNestedDocument(const std::string& inputPath) {
    static const auto empty = std::make_shared<const NestedDocument>();
    if (inputPath.empty()) return empty;
    return cache().get(featureTreeCanonicalPath(inputPath));
}

std::shared_ptr<const NestedDocument> featureTreeNestedDocument(
    const std::string& inputPath, const core::DocMirrorSnapshot& snapshot) {
    if (!snapshot.valid()) return featureTreeNestedDocument(inputPath);
    struct LiveEntry {
        std::weak_ptr<const core::DocMirrorPayload> payload;
        std::shared_ptr<const NestedDocument> document;
    };
    static std::map<std::string, LiveEntry> live;
    const std::string path = featureTreeCanonicalPath(inputPath);
    if (live.size() >= 256 && !live.count(path)) live.erase(live.begin());
    auto& entry = live[path];
    if (entry.payload.lock() != snapshot.payload) {
        entry.payload = snapshot.payload;
        entry.document = std::make_shared<NestedDocument>(parseTree(path, snapshot.tree()));
    }
    return entry.document;
}

void invalidateFeatureTreeNestedDocument(const std::string& absPath) {
    cache().invalidate(featureTreeCanonicalPath(absPath));
    ui::anim::wakeLoop();
}

}  // namespace ui
