// AICM handoff — desktop shared memory or tenant-private mesh publication.
//
// Producer (libchudware / hostd session): publish(key, bytes) returns a URI for
// a POSIX shared-memory object (Linux), named file mapping (Windows), or a
// private mesh file (tenant cells/configured publication root):
//
//   aicm-shm://<name>     POSIX shm / Windows named mapping
//   <filesystem path>     private mesh file, or desktop shm-unavailable fallback
//
// Consumer (host loadDocMesh / render::loadAicmFile): map(uri) returns bytes valid
// until unmap(). Private files use no-follow opens and read-only POSIX mappings;
// desktop fallback paths use a normal disk read.
//
// Same-process fast path: an in-process cache keeps the last publish so a same-
// address-space consumer can parse without a second open/mmap. Cross-process consumers
// open the published shm object or mesh file.
//
// Keys need only be unique within the publishing process (e.g. "docid_r12", "field_3").
// Desktop shared objects/fallback names are scoped by process id. In a tenant cell,
// CHUDWARE_CELL_STATE selects <state>/mesh instead: private files have server-minted
// opaque names unrelated to keys, and contain raw AICM bytes (no shm size header).
// Older revisions for the same doc prefix can be released via release / release_older_than.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace chudware {
namespace aicm_handoff {

// True when uri is an aicm-shm:// handoff (not a filesystem path).
bool is_shm_uri(const std::string& uri);

// Configure a private publication directory before publishing (hostd/tests).
// Empty root restores CHUDWARE_CELL_STATE/mesh, or desktop defaults if unset.
// The directory must be absolute, owned by this user, mode 0700, and symlink-free;
// a missing final component is created. Unsupported platforms fail closed.
// An invalid nonempty configuration remains selected, so publication cannot fall
// back to desktop shm/temp files. Existing publications remain releasable.
bool set_publication_root(const std::string& root, std::string& err);

// Publish bytes under `key`. Cell/configured-root mode returns an opaque .aicm
// file path, or empty on failure (never falls back outside the root). Desktop mode
// returns aicm-shm://… or a temp-file path if shm creation failed.
std::string publish(const std::string& key, const uint8_t* data, size_t n);
inline std::string publish(const std::string& key, const std::vector<uint8_t>& bytes) {
    return publish(key, bytes.data(), bytes.size());
}

// Map a handoff URI (or filesystem path) for reading. On success *data points at
// *size bytes valid until unmap(). Returns false + sets err on failure.
// In private-root mode only opaque mesh paths beneath that root are admitted,
// opened without following symlinks, and mapped read-only (cached bytes stay pinned).
// Desktop filesystem paths read into a heap buffer owned by the Mapping.
struct Mapping {
    const uint8_t* data = nullptr;
    size_t size = 0;
    // Platform private — do not touch.
    void* impl = nullptr;
};
bool map(const std::string& uri, Mapping& out, std::string& err);
void unmap(Mapping& m);

// Drop a published object (shm/file unlink + in-process cache). Safe if never published.
void release(const std::string& key);

// Release every cached/published key whose name starts with the document prefix
// and whose trailing "_rN" revision is strictly less than `keep_rev`. Used after
// a rebuild so old revision shm objects/files do not accumulate forever.
void release_older_than(const std::string& doc_id, long keep_rev);

// Release every key for this document (all revisions). Call from doc_close.
void release_doc(const std::string& doc_id);

// Release every cached publish (all docs). Call from hostd process teardown so
// kill/drain does not leave orphaned shm objects or cell mesh files. atexit-registered
// on first publish as a backstop for clean process exit.
void release_all();

// Sanitize doc_id + revision into a stable per-process key (no path separators).
std::string key_for(const std::string& doc_id, long revision);

}  // namespace aicm_handoff
}  // namespace chudware
