// Explorer thumbnails for native .step+ documents (see file_thumbs.h for the contract).
#include "file_thumbs.h"

#ifndef __EMSCRIPTEN__
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <future>
#include <memory>
#include <optional>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "miniz.h"

#include "anim.h"
#include "gl_compat.h"
#include "render/aicm_loader.h"
#include "render/doc_thumbnail.h"
#include "../ui_theme.h"   // ui::uiScale
#endif

namespace ui {

bool fileThumbSupported(const std::string& lowerExt) {
#ifdef __EMSCRIPTEN__
    (void)lowerExt;
    return false;
#else
    return lowerExt == ".step+";
#endif
}

#ifdef __EMSCRIPTEN__

FileThumb fileThumb(const std::string&, int64_t, uint64_t) { return {}; }
void fileThumbsPump() {}
bool fileThumbsBusy() { return false; }

#else

namespace {

namespace fs = std::filesystem;

constexpr size_t kMaxAicmBytes = size_t(256) << 20;   // the container's aggregate member budget
constexpr size_t kMaxJsonBytes = size_t(64) << 20;
constexpr int    kMaxDepth     = 4;                   // nested sub-assemblies
constexpr size_t kMaxTriangles = 4000000;             // bounds one frame's GPU upload
constexpr size_t kMaxEntries   = 192;                 // resident textures (~80 KiB each + mips)

// ---- background build (pure CPU, no GL, no ImGui) -------------------------------------------

struct HeapMember {
    void* data = nullptr;
    size_t size = 0;
    ~HeapMember() { if (data) mz_free(data); }
};

// Extract one member, refusing a declared size above `cap` before any allocation.
bool readMember(mz_zip_archive& z, const char* name, size_t cap, HeapMember& out) {
    const int idx = mz_zip_reader_locate_file(&z, name, nullptr, 0);
    if (idx < 0) return false;
    mz_zip_archive_file_stat stat;
    if (!mz_zip_reader_file_stat(&z, (mz_uint)idx, &stat) || stat.m_is_directory ||
        stat.m_uncomp_size > cap)
        return false;
    out.data = mz_zip_reader_extract_to_heap(&z, (mz_uint)idx, &out.size, 0);
    return out.data != nullptr;
}

nlohmann::json parseMember(mz_zip_archive& z, const char* name) {
    HeapMember m;
    if (!readMember(z, name, kMaxJsonBytes, m)) return {};
    const char* p = static_cast<const char*>(m.data);
    return nlohmann::json::parse(p, p + m.size, nullptr, /*allow_exceptions=*/false);
}

// Drop the loader's upload payloads: the thumbnail pass reads only pos/nrm/idx/faces/edges.
void slim(render::DocMesh& mesh) {
    for (auto& b : mesh.bodies) {
        b.brep_verts = {};
        b.faceIds = {};
        b.edgeLines = {};
        b.edgeIds = {};
        b.vertexPoints = {};
        b.vertexIds = {};
        b.shadedMesh = Mesh{};
    }
    mesh.faceBBoxCache.clear();
}

// Row-major rotation + translation (contracts/doc_schema.md §6).
void placeBody(const render::BodyMesh& b, const double m[16], render::BodyMesh& o) {
    o.body_id = b.body_id;
    std::copy(b.color, b.color + 3, o.color);
    o.metallic = b.metallic;
    o.roughness = b.roughness;
    o.appearance_regions = b.appearance_regions;
    o.n_verts = b.n_verts;
    o.n_tris = b.n_tris;
    o.idx = b.idx;
    o.faces = b.faces;
    o.edges = b.edges;
    auto point = [m](const std::vector<float>& in, std::vector<float>& out) {
        out.resize(in.size());
        for (size_t i = 0; i + 2 < in.size(); i += 3) {
            const double x = in[i], y = in[i + 1], z = in[i + 2];
            out[i]     = (float)(m[0] * x + m[1] * y + m[2] * z + m[3]);
            out[i + 1] = (float)(m[4] * x + m[5] * y + m[6] * z + m[7]);
            out[i + 2] = (float)(m[8] * x + m[9] * y + m[10] * z + m[11]);
        }
    };
    point(b.pos, o.pos);
    point(b.edge_pts, o.edge_pts);
    o.nrm.resize(b.nrm.size());
    for (size_t i = 0; i + 2 < b.nrm.size(); i += 3) {
        const double x = b.nrm[i], y = b.nrm[i + 1], z = b.nrm[i + 2];
        o.nrm[i]     = (float)(m[0] * x + m[1] * y + m[2] * z);
        o.nrm[i + 1] = (float)(m[4] * x + m[5] * y + m[6] * z);
        o.nrm[i + 2] = (float)(m[8] * x + m[9] * y + m[10] * z);
    }
}

using MeshPtr = std::shared_ptr<const render::DocMesh>;

// Loads one document's display mesh; assemblies compose their components. Each source file is
// parsed once per build (an assembly with twelve identical screws reads that part once).
struct Composer {
    std::unordered_map<std::string, MeshPtr> memo;
    std::unordered_set<std::string> stack;   // cycle guard
    size_t triangles = 0;

    MeshPtr load(const fs::path& path, int depth, NativeDocKind* kindOut) {
        const std::string key = path.lexically_normal().generic_u8string();
        if (auto it = memo.find(key); it != memo.end()) return it->second;
        if (depth > kMaxDepth || stack.count(key)) return nullptr;

        mz_zip_archive z;
        std::memset(&z, 0, sizeof(z));
        if (!mz_zip_reader_init_file(&z, key.c_str(), 0)) return memo[key] = nullptr;
        const nlohmann::json meta = parseMember(z, "meta.json");
        const bool assembly = meta.is_object() && meta.value("kind", std::string()) == "assembly";
        if (kindOut) *kindOut = assembly ? NativeDocKind::Assembly : NativeDocKind::Part;

        auto mesh = std::make_shared<render::DocMesh>();
        if (!assembly) {
            HeapMember aicm;
            std::string err;
            const bool ok = readMember(z, "cache.aicm", kMaxAicmBytes, aicm) &&
                            render::parseAicm(static_cast<const uint8_t*>(aicm.data), aicm.size,
                                              *mesh, err);
            mz_zip_reader_end(&z);
            if (!ok) return memo[key] = nullptr;
            slim(*mesh);
            return memo[key] = mesh;
        }

        const nlohmann::json doc = parseMember(z, "document.json");
        mz_zip_reader_end(&z);   // components are separate files; never hold this one open
        const auto comps = doc.is_object() ? doc.find("components") : doc.end();
        if (!doc.is_object() || comps == doc.end() || !comps->is_array()) return memo[key] = nullptr;

        stack.insert(key);
        for (const auto& c : *comps) {
            if (!c.is_object() || c.value("suppressed", false) || c.value("hidden", false)) continue;
            const auto src = c.find("source");
            if (src == c.end() || !src->is_object()) continue;
            const std::string rel = src->value("path", std::string());
            if (rel.empty()) continue;
            fs::path source = fs::u8path(rel);
            if (source.is_relative()) source = path.parent_path() / source;
            const MeshPtr part = load(source, depth + 1, nullptr);
            if (!part) continue;
            double m[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
            const auto t = c.find("transform");
            if (t != c.end() && t->is_array() && t->size() == 16 &&
                std::all_of(t->begin(), t->end(), [](const nlohmann::json& v) { return v.is_number(); }))
                for (int i = 0; i < 16; ++i) m[i] = (*t)[i].get<double>();
            for (const auto& b : part->bodies) {
                const size_t tris = b.idx.size() / 3;
                if (b.pos.empty() || tris == 0) continue;
                if (triangles + tris > kMaxTriangles) break;
                triangles += tris;
                mesh->bodies.emplace_back();
                placeBody(b, m, mesh->bodies.back());
            }
        }
        stack.erase(key);
        return memo[key] = mesh->empty() ? nullptr : MeshPtr(mesh);
    }
};

struct Built {
    MeshPtr mesh;
    NativeDocKind kind = NativeDocKind::Unknown;
};

Built buildThumb(const std::string& path) {
    Built out;
    try {
        Composer c;
        out.mesh = c.load(fs::u8path(path), 0, &out.kind);
    } catch (...) {   // allocation failure on a pathological file: no picture, keep the icon
        out.mesh.reset();
    }
    ui::anim::wakeLoop();
    return out;
}

// ---- GL-thread cache ------------------------------------------------------------------------

struct Entry {
    int64_t mtime = 0;
    uint64_t size = 0;
    bool needsJob = true;   // (mtime, size) changed since the last job for this entry started
    FileThumb::State state = FileThumb::State::Pending;
    NativeDocKind kind = NativeDocKind::Unknown;
    unsigned int tex = 0;
    int lastFrame = 0;
};

struct Job {
    std::string key;
    int64_t mtime = 0;
    uint64_t size = 0;
    std::future<Built> result;
};

std::unordered_map<std::string, Entry> s_entries;
std::optional<Job> s_job;

std::string entryKey(std::string path) {
    std::replace(path.begin(), path.end(), '\\', '/');
    return path;
}

void freeTexture(Entry& e) {
    if (e.tex) render::deleteDocThumbnail(e.tex);
    e.tex = 0;
}

void renderInto(Entry& e, const render::DocMesh& mesh) {
    const int px = std::clamp((int)std::lround(144.0f * ui::uiScale), 96, 288);
    std::string err;
    if (!render::renderDocThumbnail(mesh, px, px, render::DocThumbStyle{}, e.tex, err)) {
        e.state = e.tex ? FileThumb::State::Ready : FileThumb::State::Unavailable;
        return;
    }
    // Rows draw the picture at ~a quarter of its size; mips keep the edges from shimmering.
    GLint prev = 0;
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev);
    glBindTexture(GL_TEXTURE_2D, e.tex);
    glGenerateMipmap(GL_TEXTURE_2D);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR);
    glBindTexture(GL_TEXTURE_2D, (GLuint)prev);
    e.state = FileThumb::State::Ready;
}

void finishJob() {
    if (!s_job || s_job->result.wait_for(std::chrono::seconds(0)) != std::future_status::ready)
        return;
    Built built = s_job->result.get();
    const auto it = s_entries.find(s_job->key);
    // A file rewritten while its job ran keeps needsJob=true and simply runs again.
    if (it != s_entries.end() && it->second.mtime == s_job->mtime && it->second.size == s_job->size) {
        Entry& e = it->second;
        if (built.kind != NativeDocKind::Unknown) e.kind = built.kind;
        if (built.mesh) {
            renderInto(e, *built.mesh);
        } else {
            freeTexture(e);
            e.state = FileThumb::State::Unavailable;
        }
    }
    s_job.reset();
}

// Only rows drawn this frame or the last are worth building.
bool wanted(const Entry& e, int frame) { return e.needsJob && e.lastFrame >= frame - 1; }

void startJob(int frame) {
    if (s_job) return;
    auto best = s_entries.end();
    for (auto it = s_entries.begin(); it != s_entries.end(); ++it)
        if (wanted(it->second, frame) &&
            (best == s_entries.end() || it->second.lastFrame > best->second.lastFrame))
            best = it;
    if (best == s_entries.end()) return;
    best->second.needsJob = false;
    Job job;
    job.key = best->first;
    job.mtime = best->second.mtime;
    job.size = best->second.size;
    job.result = std::async(std::launch::async, buildThumb, best->first);
    s_job = std::move(job);
}

void evict(int frame) {
    if (s_entries.size() <= kMaxEntries) return;
    std::vector<std::pair<int, std::string>> idle;
    for (const auto& [key, e] : s_entries)
        if (e.lastFrame < frame - 1 && !(s_job && s_job->key == key)) idle.emplace_back(e.lastFrame, key);
    std::sort(idle.begin(), idle.end());
    for (const auto& [lastFrame, key] : idle) {
        if (s_entries.size() <= kMaxEntries) break;
        (void)lastFrame;
        auto it = s_entries.find(key);
        freeTexture(it->second);
        s_entries.erase(it);
    }
}

}  // namespace

FileThumb fileThumb(const std::string& path, int64_t mtime, uint64_t size) {
    Entry& e = s_entries[entryKey(path)];
    if (e.mtime != mtime || e.size != size) {
        e.mtime = mtime;
        e.size = size;
        e.needsJob = true;
        if (!e.tex) e.state = FileThumb::State::Pending;
    }
    e.lastFrame = ImGui::GetFrameCount();
    FileThumb out;
    out.state = e.tex ? FileThumb::State::Ready : e.state;
    out.kind = e.kind;
    out.tex = e.tex;
    return out;
}

void fileThumbsPump() {
    const int frame = ImGui::GetFrameCount();
    finishJob();
    startJob(frame);
    evict(frame);
}

bool fileThumbsBusy() {
    if (s_job) return true;
    const int frame = ImGui::GetFrameCount();
    return std::any_of(s_entries.begin(), s_entries.end(),
                       [frame](const auto& kv) { return wanted(kv.second, frame); });
}

#endif  // __EMSCRIPTEN__

}  // namespace ui
