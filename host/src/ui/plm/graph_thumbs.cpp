// PLM Graph — on-demand iso thumbnail cache for the Grid view (see
// graph_thumbs.h for the cache-key + freshness contract).
#include "graph_thumbs.h"

#include <cstdint>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <string>
#include <unordered_map>
#include <unordered_set>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "gl_compat.h"
#include "stb_image.h"

namespace ui {
// ---- shared PNG -> GL texture (declared in graph_thumbs.h) -------------------

bool plmLoadPngTexture(const std::string &pngPath, unsigned int &tex, int &w,
                       int &h) {
  int iw = 0, ih = 0, n = 0;
  unsigned char *px = stbi_load(pngPath.c_str(), &iw, &ih, &n, 4);
  if (!px || iw <= 0 || ih <= 0) {
    if (px)
      stbi_image_free(px);
    return false;
  }
  GLuint id = 0;
  glGenTextures(1, &id);
  glBindTexture(GL_TEXTURE_2D, id);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, iw, ih, 0, GL_RGBA,
               GL_UNSIGNED_BYTE, px);
  stbi_image_free(px);
  tex = (unsigned int)id;
  w = iw;
  h = ih;
  return true;
}

void plmFreePngTexture(unsigned int &tex) {
  if (!tex)
    return;
  GLuint id = (GLuint)tex;
  glDeleteTextures(1, &id);
  tex = 0;
}

namespace {

struct TexSlot {
  GLuint tex = 0;
  int w = 0, h = 0;
  double lastUse = 0.0;
};

struct ThumbJob {
  std::string key;
  std::string path; // source doc (abs)
  std::string out;  // cache png (abs)
};

std::unordered_map<std::string, TexSlot> &texCache() {
  static std::unordered_map<std::string, TexSlot> c;
  return c;
}
// Keys with a render queued or in flight this session (enqueue-once guard).
std::unordered_set<std::string> &requested() {
  static std::unordered_set<std::string> s;
  return s;
}
// Keys whose render/decode failed this session (no retry loops).
std::unordered_set<std::string> &failedKeys() {
  static std::unordered_set<std::string> s;
  return s;
}
std::deque<ThumbJob> &jobQueue() {
  static std::deque<ThumbJob> q;
  return q;
}
bool &inFlight() {
  static bool f = false;
  return f;
}

std::string thumbDir(const std::string &projectDir) {
  return (std::filesystem::path(projectDir) / ".plm" / "thumbs")
      .generic_string();
}

// FNV-1a 64 over the string — stable, dependency-free fallback key when the
// graph carries no content hash yet.
std::string fnvKey(const std::string &s) {
  uint64_t h = 1469598103934665603ull;
  for (char c : s) {
    h ^= (unsigned char)c;
    h *= 1099511628211ull;
  }
  char buf[32];
  std::snprintf(buf, sizeof(buf), "pm%016llx", (unsigned long long)h);
  return buf;
}

bool loadTextureFromFile(const std::string &pngPath, TexSlot &slot) {
  unsigned int tex = 0;
  int w = 0, h = 0;
  if (!plmLoadPngTexture(pngPath, tex, w, h))
    return false;
  slot.tex = (GLuint)tex;
  slot.w = w;
  slot.h = h;
  return true;
}

void evictLru() {
  auto &c = texCache();
  constexpr size_t kTexCap = 256;
  while (c.size() >= kTexCap) {
    auto oldest = c.begin();
    for (auto jt = c.begin(); jt != c.end(); ++jt)
      if (jt->second.lastUse < oldest->second.lastUse)
        oldest = jt;
    if (oldest->second.tex)
      glDeleteTextures(1, &oldest->second.tex);
    c.erase(oldest);
  }
}

// True when docId is currently live in a GUI tab / the active mirror — those
// docs belong to the user session and must never be doc_close'd by us.
bool isLiveDocId(AppState &st, const std::string &id) {
  if (id.empty())
    return false;
  if (st.docMirror.docId() == id)
    return true;
  for (const auto &t : st.docTabs)
    if (t.docId == id)
      return true;
  return false;
}

void finishJob(const std::string &key, bool ok) {
  inFlight() = false;
  if (!ok)
    failedKeys().insert(key);
  // On success the png is on disk; dropping the requested flag lets the next
  // ensure() pick it up (one stat + decode, then texture-map hits).
  requested().erase(key);
}

void startJob(AppState &st, const ThumbJob &job) {
  inFlight() = true;
  const std::string key = job.key;
  st.rpc.call("doc_open", nlohmann::json{{"path", job.path}},
              [&st, job, key](const core::RpcResult &r) {
                const std::string docId =
                    (r.ok && r.result.is_object())
                        ? r.result.value("doc_id", std::string())
                        : std::string();
                if (docId.empty()) {
                  finishJob(key, false);
                  return;
                }
                nlohmann::json p;
                p["doc_id"] = docId;
                p["out_path"] = job.out;
                p["annotate"] = false; // clean product shot, no callouts
                st.rpc.call(
                    "doc_render_png", p,
                    [&st, key, docId](const core::RpcResult &rr) {
                      const bool ok =
                          rr.ok && rr.result.is_object() &&
                          rr.result.value("ok", false);
                      if (!isLiveDocId(st, docId)) {
                        st.rpc.call("doc_close",
                                    nlohmann::json{{"doc_id", docId}},
                                    [](const core::RpcResult &) {});
                      }
                      finishJob(key, ok);
                    });
              });
}

} // namespace

std::string graphThumbKey(const std::string &dh, double mt,
                          const std::string &absPath) {
  if (!dh.empty())
    return dh;
  if (absPath.empty())
    return {};
  double m = mt;
  if (m <= 0.0) {
    std::error_code ec;
    auto ft = std::filesystem::last_write_time(absPath, ec);
    if (!ec)
      m = (double)ft.time_since_epoch().count();
  }
  return fnvKey(absPath + "|" + std::to_string(m));
}

GraphGridThumb graphThumbEnsure(AppState &st, const std::string &projectDir,
                                const std::string &key,
                                const std::string &absPath) {
  GraphGridThumb out;
  if (key.empty() || projectDir.empty())
    return out;

  auto &c = texCache();
  auto it = c.find(key);
  if (it != c.end()) {
    it->second.lastUse = ImGui::GetTime();
    out.tex = it->second.tex;
    out.w = it->second.w;
    out.h = it->second.h;
    return out;
  }
  if (failedKeys().count(key)) {
    out.failed = true;
    return out;
  }
  if (requested().count(key)) {
    out.pending = true; // queued or rendering — keep painting the placeholder
    return out;
  }

  const std::string pngPath = thumbDir(projectDir) + "/" + key + ".png";
  {
    std::error_code ec;
    if (std::filesystem::is_regular_file(pngPath, ec)) {
      TexSlot slot;
      slot.lastUse = ImGui::GetTime();
      if (!loadTextureFromFile(pngPath, slot)) {
        // Corrupt/partial file: treat as a miss and regenerate once.
        std::filesystem::remove(pngPath, ec);
      } else {
        evictLru();
        auto [ins, okEmplace] = c.emplace(key, std::move(slot));
        (void)okEmplace;
        out.tex = ins->second.tex;
        out.w = ins->second.w;
        out.h = ins->second.h;
        return out;
      }
    }
  }

  if (absPath.empty()) {
    out.failed = true; // nothing to render from
    failedKeys().insert(key);
    return out;
  }
  // Enqueue once per key per session.
  std::error_code ec;
  std::filesystem::create_directories(thumbDir(projectDir), ec);
  requested().insert(key);
  jobQueue().push_back(ThumbJob{key, absPath, pngPath});
  out.pending = true;
  (void)st;
  return out;
}

void graphThumbsPump(AppState &st) {
  if (inFlight() || jobQueue().empty() || !st.workerStarted)
    return;
  ThumbJob job = jobQueue().front();
  jobQueue().pop_front();
  startJob(st, job);
}

void graphThumbsReset() {
  auto &c = texCache();
  for (auto &kv : c) {
    if (kv.second.tex)
      glDeleteTextures(1, &kv.second.tex);
  }
  c.clear();
  requested().clear();
  failedKeys().clear();
  jobQueue().clear();
  inFlight() = false; // a late callback only touches the (cleared) sets
}

} // namespace ui
