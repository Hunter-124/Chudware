#pragma once
// SolidWorks / Parasolid source resolution for import.
//
// A SolidWorks part stores its finished solid as a Parasolid transmit body. The vendored sldprt
// converter (vendor/sldprt, Apache-2.0, run out of process under omp's Bun or Node) reads that
// body without SolidWorks and writes it as exact AP214 STEP. resolve_solidworks_source() picks
// the most exact lane that works on this machine and returns the file the B-rep importer reads:
//
//   .sldprt         native exact B-rep  -> SolidWorks COM STEP -> saved display tessellation
//   .sldasm         SolidWorks COM STEP -> saved display tessellation
//   .x_t / .x_b     native exact B-rep  -> SolidWorks COM STEP -> OpError("import_failed")
//
// Every lane that was tried and skipped is named in `notes`.

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::doc {

struct SolidWorksSource {
    std::string path;      // file the importer reads
    std::string format;    // "step", or "sldprt"/"sldasm" for the tessellation lane
    std::string fidelity;  // "exact_native_brep" | "solidworks_com_step" |
                           // "saved_display_tessellation"
    std::vector<std::string> notes;
    nlohmann::json provenance = nlohmann::json::object();  // native lane: saved/created release
};

// fmt is detect_format()'s key: "sldprt", "sldasm", "x_t" or "x_b".
SolidWorksSource resolve_solidworks_source(const std::string& path, const std::string& fmt);

// Native lane alone: converts `src_path` (.sldprt/.x_t/.x_b) to a temporary STEP file and returns
// the driver's JSON result ({ok, step, format, report, saved}). Throws OpError("import_failed")
// when no JavaScript runtime or converter is found, or the converter rejects the file.
nlohmann::json sldprt_native_export_step(const std::string& src_path);

// "" when the native lane can run here, else why not (no runtime / converter found).
std::string sldprt_native_unavailable_reason();

}  // namespace chudware::doc
