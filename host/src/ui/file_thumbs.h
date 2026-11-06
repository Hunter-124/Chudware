// Explorer thumbnails for native .step+ documents.
//
// Rendered on the host from the document's own saved display tessellation, with no worker
// round-trip and nothing written to disk:
//   part      -> its `cache.aicm` member (contracts/stepplus.md §1)
//   assembly  -> every visible, unsuppressed component's source part (or sub-assembly) placed
//                by its saved row-major `transform` (contracts/doc_schema.md §6)
// One background job reads + parses at a time (std::async, the Active Work preview pattern);
// the GL thread renders at most one finished mesh per frame through render::renderDocThumbnail
// and keeps only the mip-mapped texture. Only entries requested in the current or previous frame
// start a job, so a collapsed folder or a fast scroll never queues work for rows nobody sees.
// Entries are keyed by path and re-render when (mtime, size) changes; a stale picture stays up
// until its replacement lands. The browser build has no thumbnails (state Unsupported).
#pragma once

#include <cstdint>
#include <string>

namespace ui {

enum class NativeDocKind { Unknown, Part, Assembly };

struct FileThumb {
    enum class State { Unsupported, Pending, Ready, Unavailable };
    State state = State::Unsupported;
    NativeDocKind kind = NativeDocKind::Unknown;   // from meta.json, known once a job has run
    unsigned int tex = 0;                          // GL texture (top-left origin), 0 unless Ready
};

// True for extensions (lower-case, leading dot) this module renders.
bool fileThumbSupported(const std::string& lowerExt);

// Look up (and, when missing or out of date, request) the thumbnail of `path`. GL thread only.
FileThumb fileThumb(const std::string& path, int64_t mtime, uint64_t size);

// Finish/start background work and render at most one ready mesh. Call once per frame from the
// pane that draws thumbnails, before its rows.
void fileThumbsPump();

// True while any job is queued or running (headless captures wait for it).
bool fileThumbsBusy();

}  // namespace ui
