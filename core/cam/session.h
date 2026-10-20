// core/cam/session.h — the stateful CAM job document session: new / apply_ops /
// tree / generate / simulate / post / save / open, with a revision counter and
// bounded JSON-snapshot undo/redo. Mirrors the chudware::ecad::session contract so
// the host routes cam_* exactly like ecad_*.
//
// The CAM Job (core/cam/model.h) is the document. Operations carry their geometry
// inline (tests / MCP) or have it filled by the OCCT extraction adapter before
// generation. The session is OCCT-free; extraction lives in the OCCT-gated layer.
#pragma once

#include <string>
#include <cstdint>

#include <nlohmann/json.hpp>

namespace chudware {
namespace cam {

using nlohmann::json;

// Error envelope, identical shape to chudware::ecad::OpError / chudware::doc::OpError:
// {code, message, detail}. Thrown by the session; the C ABI converts to a nonzero
// return carrying the envelope.
struct OpError : std::runtime_error {
    std::string code;
    json detail;
    OpError(std::string c, const std::string& msg, json d = json::object())
        : std::runtime_error(msg), code(std::move(c)), detail(std::move(d)) {}
    json envelope() const {
        return json{{"code", code}, {"message", what()}, {"detail", detail}};
    }
};

bool dirty_document_count(std::uint64_t& count);  // nonblocking, false = unknown

// cam_new {name?, units?, post?} -> {doc_id, revision, tree}.
json cam_new(const json& params);

// cam_apply_ops {doc_id, base_revision?, ops:[...]} -> {revision, id_map, tree}.
// Ops vocabulary: add_setup, update_setup, remove_setup, add_operation,
// update_operation, remove_operation, reorder_operations, add_tool, update_tool,
// remove_tool, set_stock, set_wcs, set_machine, set_post, set_job.
json cam_apply_ops(const std::string& doc_id, long base_revision, const json& ops);

json cam_undo(const std::string& doc_id);
json cam_redo(const std::string& doc_id);
json cam_tree(const std::string& doc_id);     // {revision, tree, stats}
bool cam_close(const std::string& doc_id);
json cam_describe(const std::string& doc_id);

// cam_generate {doc_id, setup_id?, op_id?} -> {toolpaths:[Toolpath...], warnings,
// stats}. Resolves feeds, runs the generators. Generated paths are cached on the
// session for cam_simulate / cam_post. An op_id limits generation to one op.
json cam_generate(const std::string& doc_id, const json& params);

// cam_simulate {doc_id, setup_id?, options?} -> SimResult json (+ remaining field
// when requested). Requires generated toolpaths (auto-generates if stale).
json cam_simulate(const std::string& doc_id, const json& params);

// cam_post {doc_id, dialect?, options?, setup_id?} -> PostResult json (gcode text).
json cam_post(const std::string& doc_id, const json& params);

// cam_setup_sheet {doc_id, dialect?} -> {html, text}.
json cam_setup_sheet(const std::string& doc_id, const json& params);

// Stateless helpers (parity harness / MCP inline path; no session):
//   cam_generate_inline {job, setup_index?, op_id?} -> toolpaths
//   cam_post_inline {job, toolpaths, dialect, options?} -> gcode
json cam_generate_inline(const json& params);
json cam_post_inline(const json& params);

// Pure reference data for the GUI / MCP:
json cam_post_dialects();         // post_dialects_json()
json cam_materials();             // cutting DB material table
json cam_tool_types();            // tool type enum + class mapping
json cam_op_types();              // op type enum + which platforms support each

// Machine profile presets (aicad.machines/1) from MachineDB.
json cam_machines();
// cam_feeds {doc_id, setup_id?, tool_number, material?, operation?} -> Feeds::to_json
json cam_feeds(const std::string& doc_id, const json& params);
// cam_audit {doc_id, gcode?, dialect?, machine?, setup_id?} -> aicad.cam_audit/1
json cam_audit(const std::string& doc_id, const json& params);

// cam_save {doc_id, path?} -> {path}. Writes a .cam+ ZIP container.
json cam_save(const std::string& doc_id, const std::string& path);
// cam_open {path} -> {doc_id, revision, tree}.
json cam_open(const std::string& path);

// Crash recovery: drop an orphaned session-store lock held by THIS thread after a
// Windows SEH unwound past a lock guard (mirrors force_release_ecad_lock).
bool force_release_cam_lock();

}  // namespace cam
}  // namespace chudware
