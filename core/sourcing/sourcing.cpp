// core/sourcing/sourcing.cpp — the single dispatch entry the libaicad method table
// routes through. Envelopes mirror the Python worker wrappers
// (engine_oracle/sourcing_worker_rpc.py + parts_providers.WORKER_METHODS):
//   bom_rollup / bom_price  {doc_path}        -> {ok, bom:[BomLine...]}
//   request_quote           {doc_path,vendor,quantity?,material?,layers?,finish?}
//                                             -> {ok, quote} | {ok:false, reason, hint}
//   match_part_number       {mpn, generate?}  -> {ok, match} | {ok:false, error}
//   vendor_part_fetch       {supplier, part_number} -> {ok, part} | {ok:false, error}
//   spec_sheet_match        {text_or_url?, mpn?, template, dims?} -> {ok, match} | {ok:false, error}
//   list_spec_templates     {}                -> {ok, templates:[...]}
#include "sourcing/sourcing.h"

namespace aicad {
namespace sourcing {

namespace {

std::string str_param(const json& params, const char* key) {
    const json v = params.value(key, json());
    return v.is_string() ? v.get<std::string>() : std::string();
}

int int_param(const json& params, const char* key, int dflt) {
    const json v = params.value(key, json());
    if (v.is_number_integer() || v.is_number_unsigned()) return v.get<int>();
    if (v.is_number_float()) return static_cast<int>(v.get<double>());
    return dflt;
}

json require_assembly(const json& params, const Seams& seams) {
    if (!seams.load_assembly)
        throw ProviderError("no assembly loader configured (libaicad dispatch must "
                            "supply Seams::load_assembly)");
    return seams.load_assembly(str_param(params, "doc_path"));
}

}  // namespace

bool call(const std::string& method, const json& params, const Seams& seams, json& result) {
    if (method == "normalize_mpn") {
        // Deterministic MPN normalizer (spec-sheet lane) — exposed for engine-side
        // consumers that used to import the deleted spec_sheet_matcher module.
        result = {{"ok", true}, {"normalized", normalize_mpn(str_param(params, "mpn"))}};
        return true;
    }
    if (method == "estimate_made_cost") {
        // Made-part cost model ({volume_mm3, material, cycle_time_s, quantity?,
        // process?, machine_rate_per_hr?, setup_time_s?}) — the aicad.sourcing/1 §3
        // object (or §0 degraded shape); never fabricated numbers.
        try {
            result = estimate_made_cost(params);
        } catch (const std::exception& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "bom_rollup") {
        try {
            result = {{"ok", true}, {"bom", bom_rollup_lines(require_assembly(params, seams), seams)}};
        } catch (const std::exception& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "bom_price") {
        try {
            result = {{"ok", true}, {"bom", bom_price_lines(require_assembly(params, seams), seams)}};
        } catch (const std::exception& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "request_quote") {
        try {
            json quote = request_quote(str_param(params, "doc_path"), str_param(params, "vendor"),
                                       int_param(params, "quantity", 1),
                                       str_param(params, "material"),
                                       int_param(params, "layers", 2),
                                       str_param(params, "finish"), seams);
            result = {{"ok", true}, {"quote", std::move(quote)}};
        } catch (const std::exception& e) {
            // Keyless or network-down -> degrade gracefully (no fabricated price).
            result = {{"ok", false},
                      {"reason", e.what()},
                      {"hint", "Configure vendor API key to enable quoting"}};
        }
        return true;
    }
    if (method == "match_part_number") {
        try {
            const json gen = params.value("generate", json(true));
            const bool generate = !gen.is_boolean() || gen.get<bool>();
            result = {{"ok", true},
                      {"match", match_part_number(str_param(params, "mpn"), generate, seams)}};
        } catch (const std::exception& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "vendor_part_fetch") {
        try {
            result = {{"ok", true},
                      {"part", provider_fetch(str_param(params, "supplier"),
                                              str_param(params, "part_number"), seams)}};
        } catch (const ProviderError& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "spec_sheet_match") {
        try {
            const json dims = params.value("dims", json());
            const json* dims_p = dims.is_object() ? &dims : nullptr;
            result = {{"ok", true},
                      {"match", spec_sheet_match(str_param(params, "text_or_url"),
                                                 str_param(params, "mpn"),
                                                 str_param(params, "template"), dims_p, seams)}};
        } catch (const std::exception& e) {
            result = {{"ok", false}, {"error", e.what()}};
        }
        return true;
    }
    if (method == "list_spec_templates") {
        result = {{"ok", true}, {"templates", list_spec_templates()}};
        return true;
    }
    return false;
}

}  // namespace sourcing
}  // namespace aicad
