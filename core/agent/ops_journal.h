// core/agent/ops_journal.h — per-doc ops journal for telemetry traces (T-H1).
// The journal persists the bounded undo stacks for CAM and ECAD sessions to disk
// so that "undone" and "edited" events survive restarts (§2.3 of T-H1 contract).
// The part engine uses a STEP revision counter (not snapshotted here — the counter
// is accessible via Engine::revision() for stamping ai_artifact base_revision).
// This header just documents the journal convention; implementations are in
// core/cam/session.cpp and core/ecad/session.cpp (flush_cam_journal / flush_ecad_journal).
#pragma once

// Journal file naming convention:
//   CAM:  <doc_path_without_ext>.cam_journal.jsonl
//   ECAD: <doc_path_without_ext>.ecad_journal.jsonl
// Format: one JSON object per line, newest snapshot last.
// Bound: CAM 50 snapshots, ECAD 100 snapshots (mirrors in-memory limits).
// Location: NEXT TO the .step+ / .ecad+ container, NOT inside the ZIP.
// The exporter (T-H5) reads these journals to build correction traces.
namespace aicad {
namespace agent {
constexpr int kCamJournalLimit  = 50;
constexpr int kEcadJournalLimit = 100;
}  // namespace agent
}  // namespace aicad
