// core/parts/parts.h — parametric standard-parts catalog.
//
// Generates real, EDITABLE geometry for common off-the-shelf hardware (fasteners,
// nuts, washers, standoffs, bearings, inserts, dowels) as a list of DOCUMENT OPS —
// the same closed vocabulary the modeler/GUI emit. The ops are fed to
// chudware::doc::doc_new + doc_apply_ops, so a generated part is a normal feature tree
// (primitives + sketch/extrude + boolean-by-mode), not a frozen import. This keeps
// the module OCCT-free (it only emits JSON); the doc engine compiles the geometry.
//
// All dimensions are millimetres (imperial sizes are pre-converted to mm). The
// numbers mirror agent/prompts/shared/cheatsheets/hardware.md (single source of
// truth for the engineer-facing catalog).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware {
namespace parts {

using nlohmann::json;

// One catalog family (a kind of part). `sizes` are selectable designations
// (e.g. "M3" or "1/4-20"); `lengths_mm` are suggested lengths when needs_length.
struct Family {
    std::string key;          // stable id, e.g. "shcs"
    std::string name;         // human label, e.g. "Socket Head Cap Screw"
    std::string category;     // grouping, e.g. "Screws"
    std::string standard;     // governing standard, e.g. "ISO 4762 / DIN 912"
    std::string description;  // one line
    bool needs_length = false;
    std::vector<std::string> sizes;
    std::vector<double> lengths_mm;
};

// All families, ordered for display.
std::vector<Family> catalog();

// JSON form for the parts_catalog_list RPC: {"families":[{key,name,category,
// standard,description,needs_length,sizes,lengths_mm}, ...]}.
json catalog_json();

// Comma-separated catalog family keys (e.g. "shcs, bhcs, …") for error strings.
std::string family_keys_csv();

// The generated ops + metadata for one concrete part.
struct PartBuild {
    std::string name;  // display / file name, e.g. "SHCS M3x10"
    json ops;          // JSON array of document ops (doc_apply_ops input)
    json meta;         // {family,size,length_mm,specs:{...}} echoed back
};

// Build the ops for a concrete part. `length_mm` is ignored for families whose
// needs_length is false. Throws std::invalid_argument on an unknown family or size.
PartBuild build(const std::string& family, const std::string& size,
                double length_mm, const json& options);

}  // namespace parts
}  // namespace chudware
