// Native (no LibreDWG, no ODA) reader for the ACIS-backed entities (3DSOLID, BODY, REGION and
// the SURFACE class family) of AutoCAD DWG files R13 through R2018, written from the public
// Open Design Specification for .dwg files (v5.4.1).
//
// Containers: AC1012/AC1014/AC1015 (R13/R14/R2000) are flat files located through the
// section-locator records; AC1018/AC1024/AC1027/AC1032 use the R2004 container (encrypted
// file header, page map, section map, masked + checksummed data pages, LZ77); AC1021 (R2007)
// uses its own container (Reed-Solomon interleaved header and pages, CRC64 checks, R21
// compression). Objects are decoded in each version's encoding (R13/R14, R2000, R2004, R2007
// with its string stream, R2010+) for type, owner block record and layer; every object whose
// contents are returned is checked against its CRC.
//
// ACIS payload (`data`): R2013+ stores SAB in the data-storage section AcDb:AcDsPrototype_1b,
// keyed by the owning entity's handle; it is returned from its "ASM BinaryFile4" / "ACIS
// BinaryFile" signature through the End-of-ASM-data / End-of-ACIS-data marker. Earlier
// versions (and R2013+ entities without a data-store record) store it inside the entity:
// version 1 is SAT text in blocks of at most 4096 bytes, returned de-obfuscated with the
// blocks joined exactly (a block boundary may split a token, which is why dwg2dxf output is
// not used) and CR LF / CR line ends turned into LF; version 2 is SAB (or SAT text) returned
// through its end marker. Pre-R13 drawings (AC1009 and older) cannot hold ACIS: `version`
// only, empty list, no error.
//
// 2D geometry stays with the dwg2dxf path; this reader never writes DWG and never builds
// B-rep (core/doc/acis_read.h turns `data` into shapes). Every size read from the file is
// bounded by the file or section that holds it; anything malformed throws std::runtime_error
// naming the structure and offset.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace chudware::doc {

struct DwgAcisEntity {
    std::uint64_t handle = 0;
    std::string type;               // "3DSOLID" | "BODY" | "REGION" | "SURFACE" | "PLANESURFACE" | ...
    std::uint64_t owner = 0;        // owning BLOCK_RECORD handle (model space, a block, paper space)
    std::uint64_t layer = 0;        // LAYER object handle
    std::string data;               // SAB (or SAT) bytes for this entity; empty if it has none
};

struct DwgNativeInfo {
    std::string version;            // "AC1032" etc.
    std::vector<DwgAcisEntity> acis; // every ACIS-backed entity, file (handle) order
    std::vector<std::string> warnings;
    std::uint64_t model_space = 0;  // *Model_Space BLOCK_RECORD handle; 0 for pre-R13 files
};

// Reads R13..R2018 (AC1012, AC1014, AC1015, AC1018, AC1021, AC1024, AC1027, AC1032). Pre-R13
// files return an empty `acis` list without error. Throws std::runtime_error with a named
// reason on a corrupt container or object, or an unknown version.
DwgNativeInfo read_dwg_native(const std::string& path);

// --- Building blocks (exposed for unit tests) ---------------------------------------------------

// R2004 LZ77 page decompressor (spec §4.7). Produces at most `max_out` bytes; throws when the
// stream is malformed, back-references before the output start, or would exceed `max_out`.
std::string dwg_decompress_r2004(std::string_view src, std::size_t max_out);

// One live record of a decompressed AcDb:AcDsPrototype_1b section (spec chapter 24).
struct DwgAcdsRecord {
    std::uint64_t handle = 0;  // owning object handle from the data record header
    std::string schema;        // schema name, e.g. "AcDb3DSolid_ASM_Data", "AcDb_Thumbnail_Schema"
    bool asm_data = false;     // schema's only payload property is the binary "ASM_Data"
    std::string data;          // record bytes; blob01 pages already concatenated
};

// Every record the data index references, in data-index order. Stale records left in data
// segments by earlier saves are not referenced by the index and are not returned.
std::vector<DwgAcdsRecord> read_dwg_acds(std::string_view section);

}  // namespace chudware::doc
