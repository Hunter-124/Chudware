// core/sourcing/sourcing.h — BOM roll-up, pricing, made-part cost model, spec-sheet
// matching, and vendor/quote providers. C++ port of the Python oracle modules
//   engine_oracle/bom.py                 (rollup / resolve_prices / to_dicts)
//   engine_oracle/cost_model.py          (estimate_made_cost + material seed)
//   engine_oracle/spec_sheet_matcher.py  (deterministic lane: normalize/match/templates)
//   engine_oracle/parts_providers.py     (Nexar / Digi-Key / Mouser pricing chain)
//   engine_oracle/quote_providers.py     (Xometry / JLCPCB quote-out)
// backing the worker methods bom_rollup, bom_price, request_quote, match_part_number,
// vendor_part_fetch and the MCP spec_sheet_match lane (contracts/sourcing_schema.md).
//
// The library is PURE: no OCCT, no doc/ecad/agent linkage. Every side effect
// (assembly/board loading, vendor-registry lookup, HTTP transport, LLM extraction,
// model building) is an injectable seam on `Seams` — the libaicad dispatch glue
// supplies the production defaults (doc_open/doc_tree, ecad_container_open,
// agent::http_post), tests inject in-memory fakes, exactly like the Python
// oracle's injectable loaders. Network calls NEVER happen without configured
// credentials (contract §0: providers fail closed); secrets come from the user
// config / environment only, never from code.
#pragma once

#include <functional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad {
namespace sourcing {

using nlohmann::json;

// ---- errors (carry .message like the Python ProviderError/QuoteError/CostModelError) ----
struct ProviderError : std::runtime_error { using std::runtime_error::runtime_error; };
struct QuoteError : std::runtime_error { using std::runtime_error::runtime_error; };
struct CostError : std::runtime_error { using std::runtime_error::runtime_error; };

// ---- HTTP seam ---------------------------------------------------------------------------
// Mirrors core/agent/http.h's HttpResponse shape without linking the agent lib: the
// dispatch glue wraps agent::http_post/http_get into these std::functions. An empty
// function means "no transport" — every provider then fails closed (unconfigured).
struct HttpResult {
    long status = 0;        // HTTP status, or 0 on a transport failure
    std::string body;       // response payload (UTF-8)
    std::string error;      // non-empty only on a transport failure
};
using HttpHeaders = std::vector<std::pair<std::string, std::string>>;
using HttpPostFn =
    std::function<HttpResult(const std::string& url, const HttpHeaders& headers,
                             const std::string& body)>;
using HttpGetFn =
    std::function<HttpResult(const std::string& url, const HttpHeaders& headers)>;

// ---- injectable seams (mirror the Python oracle's load_assembly/load_board/… kwargs) ------
struct Seams {
    // assembly doc_path -> the doc_tree envelope ({"revision","tree",...}).
    std::function<json(const std::string& doc_path)> load_assembly;
    // board path (.ecad/.ecad+) -> the board document json, or a null json on failure.
    std::function<json(const std::string& path)> load_board;
    // component json -> a registered VendorPart record, or null (Python: vendor_lookup).
    std::function<json(const json& component)> vendor_lookup;
    // mpn -> a priced VendorPart record, or null. Empty -> the Nexar/Digi-Key/Mouser
    // chain over the http seams (which fails closed with no credentials/transport).
    std::function<json(const std::string& mpn)> price_fetch;
    // bolts `match` dict -> saved model path ("" when not buildable). Python default
    // routes through the parts_make core RPC + doc_save; empty here -> model_path null.
    std::function<std::string(const json& match)> build_model;
    // template name + dims -> saved model path for the spec-sheet register lane.
    std::function<std::string(const std::string& tmpl, const json& dims)> build_spec_model;
    // (datasheet text, template) -> extracted dims object — the LLM lane. Empty in the
    // pure core: spec_sheet_match then degrades to the contract "unknown" shape unless
    // the caller passes pre-extracted `dims` in params.
    std::function<json(const std::string& text, const std::string& tmpl)> extract_dims;

    HttpPostFn http_post;
    HttpGetFn http_get;
};

// ---- BOM (engine_oracle/bom.py) ------------------------------------------------------------
// Roll the assembly tree (+ any linked ECAD boards) into unpriced BomLine dicts —
// the exact `to_dicts(rollup(...))` shape (aicad.sourcing/1 §3 + mpn/description).
json bom_rollup_lines(const json& asm_tree, const Seams& seams);
// rollup + resolve_prices (bought lines priced via the provider chain; unpriced
// lines keep unit_cost/ext_cost null — never a fabricated number).
json bom_price_lines(const json& asm_tree, const Seams& seams);
// Price the bought lines of `bom` in place (Python: resolve_prices).
void resolve_prices(json& bom, const Seams& seams);
// slug() exactly as engine/vendor_parts.py::slug (vendor index keys).
std::string vendor_slug(const std::string& value);
// Highest price_breaks tier whose qty <= quantity, else unit_price; null when none.
json unit_price_for_qty(const json& vendor_part, int quantity);

// ---- cost model (engine_oracle/cost_model.py) ----------------------------------------------
double rate_per_kg(const std::string& material);      // throws CostError on unknown
double density_g_cm3(const std::string& material);    // throws CostError on unknown
double material_cost(double volume_mm3, const std::string& material);
double machining_cost(double cycle_time_s, double machine_rate_per_hr);
// The aicad.sourcing/1 §3 cost_model object (or the §0 degraded shape). Params:
// {volume_mm3, material, cycle_time_s, quantity?, process?, machine_rate_per_hr?,
//  setup_time_s?} — null/missing inputs degrade exactly like the Python oracle.
json estimate_made_cost(const json& params);
// Python round(x, 6) — round-half-even at 6 decimals (NOT std::round).
double round6(double v);

// ---- spec-sheet deterministic lane (engine_oracle/spec_sheet_matcher.py) --------------------
std::string normalize_mpn(const std::string& pn);
// BOLTS/ISO lookup -> match dict, or null json (never a guess).
json match_bolts(const std::string& mpn);
// normalize -> bolts -> (kicad lane NOT PORTED: needs the deferred .kicad_sym parser).
// `generate` routes a bolts hit through seams.build_model (null -> model_path null).
json match_part_number(const std::string& mpn, bool generate, const Seams& seams);
json list_spec_templates();
std::string gsl_preview(const json& dims, const std::string& tmpl, const std::string& mpn);
// The doc ops a filled template builds (box/cylinder primitives) — for build_model glue.
// Throws ProviderError on an unknown template or a missing required dim.
json template_build_ops(const std::string& tmpl, const json& dims);
// Build + register lane. Throws ProviderError on unknown template / missing mpn /
// missing required dims (Python: ValueError — the dispatch maps it to ok:false).
json register_from_spec_sheet(const json& dims, const std::string& tmpl,
                              const std::string& mpn, const std::string& description,
                              const Seams& seams);
// Full path. `dims` non-null skips LLM extraction (pre-extracted dims); otherwise the
// extract_dims seam is used, and without it the contract "unknown" shape is returned
// with a reason naming the unported LLM lane.
json spec_sheet_match(const std::string& text_or_url, const std::string& mpn,
                      const std::string& tmpl, const json* dims, const Seams& seams);

// ---- vendor providers (engine_oracle/parts_providers.py) ------------------------------------
// The default pricing chain Nexar -> Digi-Key -> Mouser over the http seams; null when
// every provider declines (keyless/transport/no-result — the §0 "price unknown" state).
json fetch_price(const std::string& mpn, const Seams& seams);
// Dispatch to one named provider (aliases like "digi-key"/"octopart" resolve).
// Throws ProviderError on unknown supplier / unconfigured / no results.
json provider_fetch(const std::string& supplier, const std::string& part_number,
                    const Seams& seams);

// ---- quote-out (engine_oracle/quote_providers.py) -------------------------------------------
// STEP/Gerber path + vendor -> aicad.sourcing/1 §4 QuoteResult, or the §0 degraded
// {"ok":false,"provider","reason","hint"} shape. Never raises for missing keys.
json request_quote(const std::string& part_file, const std::string& vendor, int quantity,
                   const std::string& material, int layers, const std::string& finish,
                   const Seams& seams);
// base64(HMAC-SHA256(secret, METHOD\npath\ntimestamp\nnonce\nbody)) — exposed for tests.
std::string jlcpcb_sign(const std::string& method, const std::string& path,
                        const std::string& timestamp, const std::string& nonce,
                        const std::string& body, const std::string& secret_key);

// ---- config -----------------------------------------------------------------------------------
// The user config.json path — mirrors agent/paths.cpp::paths_config (same APPDATA /
// XDG / legacy-AI-CAD pick logic) so the C++ providers read the SAME file the Python
// engine reads. Exposed for tests (they point XDG_DATA_HOME at a fixture dir).
std::string config_path();
json load_config();  // {} on any read/parse failure (never hard-fails)

// ---- vendor registry (engine_oracle/vendor_parts.py) -------------------------------------------
// %APPDATA%/AI-CAD/vendor_parts (mirror of engine/paths.py::VENDOR_PARTS).
std::string vendor_parts_dir();
// Register a local CAD file in the vendor-parts index (port of register_local_cad with
// copy_file=True): copies the file under vendor_parts/<slug>/<slug>/, writes the record
// into index.json, returns the asdict-shaped record. Throws on a missing CAD file.
json register_local_cad(const std::string& supplier, const std::string& part_number,
                        const std::string& cad_path, const std::string& description,
                        const std::string& product_url, const json& specs,
                        const std::string& source);

// ---- single dispatch entry --------------------------------------------------------------------
// Worker-method surface: bom_rollup, bom_price, request_quote, match_part_number,
// vendor_part_fetch, spec_sheet_match, list_spec_templates. Returns false when the
// method is not ours (caller falls through). Result envelopes mirror the Python
// worker wrappers in engine_oracle/sourcing_worker_rpc.py.
bool call(const std::string& method, const json& params, const Seams& seams, json& result);

}  // namespace sourcing
}  // namespace aicad
