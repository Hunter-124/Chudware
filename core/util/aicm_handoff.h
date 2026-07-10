// AICM shared-memory handoff — avoid a temp-file round-trip on every rebuild.
//
// Producer (libaicad / hostd session): publish_aicm(key, bytes) writes the blob into
// a POSIX shared-memory object (Linux) or named file mapping (Windows) and returns a
// URI the consumer can open without touching disk:
//
//   aicm-shm://<name>     POSIX shm / Windows named mapping
//   <filesystem path>     fallback when shm is unavailable
//
// Consumer (host loadDocMesh / render::loadAicmFile): map_aicm(uri) mmap's the same
// object and returns a pointer for parseAicm; unmap_aicm releases the mapping.
// File-path URIs still use a normal disk read (fallback path).
//
// Same-process fast path: an in-process cache keeps the last publish so a same-
// address-space consumer can parse without a second open/mmap. Cross-process (hostd
// → GUI) always goes through the named shm object.
//
// Keys should be unique per doc+revision (e.g. "docid_r12"); non-alnum chars are
// sanitized. Older revisions for the same doc prefix can be released via
// release_aicm / release_older_than.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace aicad {
namespace aicm_handoff {

// True when uri is an aicm-shm:// handoff (not a filesystem path).
bool is_shm_uri(const std::string& uri);

// Publish bytes under `key`. Returns aicm-shm://… on success, or a temp-file path
// if shm creation failed (caller can still hand that path to loadAicmFile). Empty
// string only on hard failure (empty input / cannot write anything).
std::string publish(const std::string& key, const uint8_t* data, size_t n);
inline std::string publish(const std::string& key, const std::vector<uint8_t>& bytes) {
    return publish(key, bytes.data(), bytes.size());
}

// Map a handoff URI (or filesystem path) for reading. On success *data points at
// *size bytes valid until unmap(). Returns false + sets err on failure.
// For filesystem paths this reads the whole file into a heap buffer owned by the
// Mapping (still released by unmap).
struct Mapping {
    const uint8_t* data = nullptr;
    size_t size = 0;
    // Platform private — do not touch.
    void* impl = nullptr;
};
bool map(const std::string& uri, Mapping& out, std::string& err);
void unmap(Mapping& m);

// Drop a published object (shm unlink + in-process cache). Safe if never published.
void release(const std::string& key);

// Release every cached/published key whose name starts with `prefix` and whose
// trailing "_rN" revision is strictly less than `keep_rev`. Used after a rebuild
// so old revision shms don't accumulate forever.
void release_older_than(const std::string& doc_id, long keep_rev);

// Release every key for this document (all revisions). Call from doc_close.
void release_doc(const std::string& doc_id);

// Sanitize doc_id + revision into a stable shm key (no path separators).
std::string key_for(const std::string& doc_id, long revision);

}  // namespace aicm_handoff
}  // namespace aicad
