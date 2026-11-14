#pragma once
// Persistent, content-keyed assembly cache under the user's app-data cache directory.
//
// An assembly's own cache.aicm stops at the STEP+ archive budget (a security limit for
// untrusted archives), and a nested sub-assembly stores no solved geometry of its own, so
// every cold process used to re-solve nested assemblies and re-mesh every component. This
// store keeps those results beside the user's other private state instead:
//   - "mesh":   a component's local-frame display mesh (identity placement)
//   - "nested": a nested sub-assembly's solved, placed bodies
// Keys are content identities (component_cache_key: path + document hashes of the source
// and every descendant + the stamps of any non-native input), salted with the engine's
// geometry epoch, so an edit anywhere below a component selects a different entry; stale
// entries are never revalidated, only aged out. Entries are checksummed and published by
// atomic rename, so concurrent processes (hostd, MCP in-process engines) can share the
// directory. Total size is capped with least-recently-used eviction by file mtime.
//
// Location: CHUDWARE_ASM_CACHE_DIR, else <cell state>/cache/assembly in a rooted cell,
// else %LOCALAPPDATA%\Chudware\cache\assembly (Windows) or
// ${XDG_CACHE_HOME:-~/.cache}/chudware/assembly. CHUDWARE_ASM_CACHE=0 disables it, and
// CHUDWARE_ASM_CACHE_MAX_MB sets the cap (default 2048). Every failure is a cache miss.

#include <cstdint>
#include <string>

#include "geom/aicm.h"

namespace chudware::doc::asm_disk_cache {

// Payload for (kind, key), or false on a miss / corrupt entry. A hit refreshes the entry's age.
bool get(const std::string& kind, const std::string& key, std::string& payload);
// Store best-effort, then evict the oldest entries while the store exceeds its cap.
void put(const std::string& kind, const std::string& key, const std::string& payload);

// Byte encoding of one AICM body for the "mesh" kind (same-build layout, guarded by sizes).
std::string encode_body(const aicm::AicmBody& body);
bool decode_body(const std::string& bytes, aicm::AicmBody& body);

// Test hooks: the resolved directory ("" when disabled) and the store's total bytes.
std::string directory();
std::uint64_t total_bytes();

}  // namespace chudware::doc::asm_disk_cache
