// core/tests/test_sourcing.cpp — sourcing/BOM/cost/spec-match/providers (OCCT-free,
// offline). Mirrors the Python oracle's fixtures: BOM roll-up + price resolution
// (engine_oracle/test_bom.py), the cost model (test_cost_model.py), deterministic
// spec matching (test_spec_sheet_matcher.py), provider mapping (test_parts_providers.py)
// and quote signing (test_quote_providers.py). All side effects are Seams fakes —
// no network, no doc engine, no DLL.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include "sourcing/sourcing.h"

using namespace chudware::sourcing;
using nlohmann::json;

// Portable env helpers (MSVC has no POSIX setenv/unsetenv; matches the
// _putenv_s convention used across the other core/tests/*.cpp files).
static void test_setenv(const char* k, const char* v) {
#ifdef _WIN32
    _putenv_s(k, v);
#else
    setenv(k, v, 1);
#endif
}
static void test_unsetenv(const char* k) {
#ifdef _WIN32
    _putenv_s(k, "");
#else
    unsetenv(k);
#endif
}

// ---------------------------------------------------------------------------
// Fixture assembly tree + board (shared by the rollup / pricing suites).
// ---------------------------------------------------------------------------

static json fixture_asm_tree() {
    return json::parse(R"({
      "revision": 3,
      "tree": {"components": [
        {"id":"k1","name":"bracket","source":{"path":"/parts/bracket.step"},"quantity":1},
        {"id":"k2","name":"bracket","source":{"path":"/parts/bracket.step"},"quantity":1},
        {"id":"k3","source":{"path":"/parts/screw.step"},"mpn":"ABC-123","supplier":"Digi-Key","quantity":1},
        {"id":"k4","source":{"path":"/parts/screw.step"},"mpn":"ABC-123","supplier":"Digi-Key","quantity":1},
        {"id":"k5","name":"board","source":{"path":"/boards/main.ecad+"}}
      ]}
    })");
}

static json fixture_board() {
    return json::parse(R"({
      "footprints": [
        {"ref":"R10","value":"10k","fpid":"Resistor_SMD:R_0603","fields":{"MPN":"RC0603FR-0710KL"}},
        {"ref":"R2","value":"10k","fpid":"Resistor_SMD:R_0603","fields":{"MPN":"RC0603FR-0710KL"}},
        {"ref":"R1","value":"10k","fpid":"Resistor_SMD:R_0603","fields":{"MPN":"RC0603FR-0710KL"}},
        {"ref":"C1","value":"100nF","fpid":"C_0603","dnp":true,"fields":{"MPN":"GRM188"}},
        {"ref":"U1","value":"MCU","fpid":"QFN-48","attr":"virtual"},
        {"ref":"C2","value":"1uF","fpid":"C_0603"}
      ]
    })");
}

static Seams fixture_seams() {
    Seams s;
    s.load_board = [](const std::string&) { return fixture_board(); };
    return s;
}

TEST_CASE("bom rollup: made/bought classification, aggregation, board merge") {
    const json bom = bom_rollup_lines(fixture_asm_tree(), fixture_seams());
    REQUIRE(bom.is_array());
    REQUIRE(bom.size() == 4);

    // L1: the two identical brackets aggregate into one made line.
    CHECK(bom[0]["line_id"] == "L1");
    CHECK(bom[0]["kind"] == "made");
    CHECK(bom[0]["quantity"] == 2);
    CHECK(bom[0]["description"] == "bracket");
    CHECK(bom[0]["part_ref"].is_null());
    CHECK(bom[0]["vendor_part"].is_null());
    CHECK(bom[0]["cost_model"].is_null());
    CHECK(bom[0]["unit_cost"].is_null());
    CHECK(bom[0]["ext_cost"].is_null());
    CHECK(bom[0]["currency"] == "USD");
    CHECK(bom[0]["mpn"].is_null());

    // L2: the two Digi-Key screws aggregate into one bought line with a vendor key.
    CHECK(bom[1]["line_id"] == "L2");
    CHECK(bom[1]["kind"] == "bought");
    CHECK(bom[1]["quantity"] == 2);
    CHECK(bom[1]["part_ref"] == "digi-key::abc-123");
    CHECK(bom[1]["mpn"] == "ABC-123");
    CHECK(bom[1]["description"] == "/parts/screw.step");

    // L3/L4: the board expands to grouped electronic lines (DNP C1 and virtual U1
    // excluded; refs natural-sorted; C2 sorts before R1).
    CHECK(bom[2]["line_id"] == "L3");
    CHECK(bom[2]["kind"] == "bought");
    CHECK(bom[2]["quantity"] == 1);
    CHECK(bom[2]["description"] == "1uF C_0603");
    CHECK(bom[2]["mpn"].is_null());

    CHECK(bom[3]["line_id"] == "L4");
    CHECK(bom[3]["quantity"] == 3);
    CHECK(bom[3]["description"] == "10k Resistor_SMD:R_0603");
    CHECK(bom[3]["mpn"] == "RC0603FR-0710KL");
}

TEST_CASE("bom rollup: non-assembly tree yields an empty BOM") {
    CHECK(bom_rollup_lines(json::object(), Seams{}).empty());
    CHECK(bom_rollup_lines(json::parse(R"({"tree":{"features":[]}})"), Seams{}).empty());
}

TEST_CASE("bom price: provider-chain pricing folds into bought lines") {
    Seams s = fixture_seams();
    s.price_fetch = [](const std::string& mpn) -> json {
        if (mpn != "ABC-123") return json();
        return json::parse(R"({
          "supplier":"Digi-Key","part_number":"ABC-123","description":"screw",
          "unit_price":0.5,"currency":"USD",
          "price_breaks":[{"qty":1,"unit_price":0.5},{"qty":10,"unit_price":0.4}],
          "stock":900,"lead_time_days":null,"moq":1
        })");
    };
    const json bom = bom_price_lines(fixture_asm_tree(), s);
    REQUIRE(bom.size() == 4);
    // The priced bought line: qty 2 -> the qty-1 tier (0.5), ext 1.0.
    CHECK(bom[1]["unit_cost"] == doctest::Approx(0.5));
    CHECK(bom[1]["ext_cost"] == doctest::Approx(1.0));
    CHECK(bom[1]["currency"] == "USD");
    CHECK(bom[1]["vendor_part"]["supplier"] == "Digi-Key");
    // The unpriced board lines keep null prices (never a fabricated number).
    CHECK(bom[3]["unit_cost"].is_null());
    CHECK(bom[3]["ext_cost"].is_null());
    CHECK(bom[3]["vendor_part"].is_null());
    // Made lines are untouched.
    CHECK(bom[0]["unit_cost"].is_null());
}

TEST_CASE("unit price for qty: highest tier at-or-below qty, else unit_price") {
    const json rec = json::parse(
        R"({"unit_price":0.9,"price_breaks":[{"qty":10,"unit_price":0.4},
            {"qty":1,"unit_price":0.5},{"qty":100,"unit_price":0.3}]})");
    CHECK(unit_price_for_qty(rec, 1).get<double>() == doctest::Approx(0.5));
    CHECK(unit_price_for_qty(rec, 12).get<double>() == doctest::Approx(0.4));
    CHECK(unit_price_for_qty(rec, 250).get<double>() == doctest::Approx(0.3));
    const json flat = json::parse(R"({"unit_price":0.9,"price_breaks":[{"qty":50,"unit_price":0.4}]})");
    CHECK(unit_price_for_qty(flat, 3).get<double>() == doctest::Approx(0.9));
    const json none = json::parse(R"({"price_breaks":[]})");
    CHECK(unit_price_for_qty(none, 3).is_null());
}

TEST_CASE("vendor slug mirrors engine/vendor_parts.py::slug") {
    CHECK(vendor_slug("Digi-Key") == "digi-key");
    CHECK(vendor_slug("ABC-123") == "abc-123");
    CHECK(vendor_slug("  Some  Supplier! ") == "some-supplier");
    CHECK(vendor_slug("###") == "part");
}

// ---------------------------------------------------------------------------
// Cost model (engine_oracle/cost_model.py).
// ---------------------------------------------------------------------------

TEST_CASE("cost model: made-part estimate matches the oracle formula") {
    // aluminum: density 2.70 g/cm3, rate 3.5 $/kg; machine rate 75 $/hr; setup 900 s.
    const json r = estimate_made_cost(json::parse(
        R"({"volume_mm3":10000.0,"material":"aluminum","cycle_time_s":600.0})"));
    REQUIRE(r["ok"] == true);
    CHECK(r["material_cost"] == doctest::Approx(0.0945));   // 0.027 kg * 3.5
    CHECK(r["machine_cost"] == doctest::Approx(12.5));      // 600/3600*75
    CHECK(r["setup_cost"] == doctest::Approx(18.75));       // 900/3600*75 / 1
    CHECK(r["unit_cost"] == doctest::Approx(31.3445));
    CHECK(r["currency"] == "USD");
    CHECK(r["source"] == "model");
    CHECK(r["process"] == "cnc_milling");
    CHECK(r["inputs"]["quantity"] == 1);
    CHECK(r["provenance"]["rate_usd_per_kg"] == doctest::Approx(3.5));
    CHECK(r["provenance"]["density_g_cm3"] == doctest::Approx(2.70));
    CHECK(r["provenance"]["asof"] == "2026-01");
}

TEST_CASE("cost model: setup amortizes across quantity; aliases resolve") {
    const json r = estimate_made_cost(json::parse(
        R"({"volume_mm3":10000.0,"material":"6061","cycle_time_s":600.0,"quantity":4})"));
    REQUIRE(r["ok"] == true);
    CHECK(r["material"] == "6061");
    CHECK(r["setup_cost"] == doctest::Approx(18.75 / 4));
    CHECK(r["unit_cost"] == doctest::Approx(0.0945 + 12.5 + 18.75 / 4));
    CHECK(r["inputs"]["quantity"] == 4);
}

TEST_CASE("cost model: unknown material and missing inputs degrade, never guess") {
    const json unknown = estimate_made_cost(json::parse(
        R"({"volume_mm3":1000.0,"material":"unobtainium","cycle_time_s":60.0})"));
    CHECK(unknown["ok"] == false);
    CHECK(unknown["unit_cost"].is_null());
    CHECK(std::string(unknown["reason"]).find("unknown material") != std::string::npos);

    const json missing = estimate_made_cost(json::parse(R"({"material":"steel"})"));
    CHECK(missing["ok"] == false);
    CHECK(std::string(missing["reason"]) ==
          "cost model missing input(s): volume_mm3, cycle_time_s");
}

// ---------------------------------------------------------------------------
// Spec-sheet deterministic lane (engine_oracle/spec_sheet_matcher.py).
// ---------------------------------------------------------------------------

TEST_CASE("normalize_mpn strips vendor/packaging suffixes") {
    CHECK(normalize_mpn("M3x8-ND") == "M3x8");
    CHECK(normalize_mpn("608-2RS-CT-ND") == "608-2RS");
    CHECK(normalize_mpn("PART-100") == "PART-100");       // bare numeric survives
    CHECK(normalize_mpn("ABC-123-ND") == "ABC-123");      // 3-digit qty keeps -123
    CHECK(normalize_mpn("GRM188(Cut Tape)") == "GRM188");
    CHECK(normalize_mpn("M4x20-1-ND") == "M4x20");
    CHECK(normalize_mpn("  6205-2RS  ") == "6205-2RS");
}

TEST_CASE("match_bolts: metric fastener, bearing, family hint, explicit standard") {
    const json m3 = match_bolts("M3x8");
    REQUIRE(!m3.is_null());
    CHECK(m3["source"] == "bolts");
    CHECK(m3["standard"] == "ISO 4762");
    CHECK(m3["family"] == "shcs");
    CHECK(m3["size"] == "M3");
    CHECK(m3["length_mm"] == doctest::Approx(8.0));
    CHECK(m3["catalog_id"] == "ISO 4762 M3x8");
    CHECK(m3["params"]["head_d"] == doctest::Approx(5.5));
    CHECK(m3["needs_length"] == true);

    const json brg = match_bolts("608-2RS");
    REQUIRE(!brg.is_null());
    CHECK(brg["standard"] == "ISO 15");
    CHECK(brg["family"] == "bearing");
    CHECK(brg["size"] == "608");
    CHECK(brg["length_mm"].is_null());
    CHECK(brg["params"]["outer_d"] == doctest::Approx(22.0));
    CHECK(brg["catalog_id"] == "ISO 15 608");

    const json nut = match_bolts("M5 hex nut");
    REQUIRE(!nut.is_null());
    CHECK(nut["standard"] == "ISO 4032");
    CHECK(nut["family"] == "hex_nut");

    const json expl = match_bolts("ISO 4762 M4x20");
    REQUIRE(!expl.is_null());
    CHECK(expl["size"] == "M4");
    CHECK(expl["length_mm"] == doctest::Approx(20.0));

    CHECK(match_bolts("XYZ-999").is_null());
    CHECK(match_bolts("").is_null());
}

TEST_CASE("match_part_number: hit/miss contract shapes") {
    const json hit = match_part_number("M3x8-ND", false, Seams{});
    CHECK(hit["matched"] == true);
    CHECK(hit["mpn"] == "M3x8-ND");
    CHECK(hit["normalized"] == "M3x8");
    CHECK(hit["catalog_id"] == "ISO 4762 M3x8");
    CHECK(hit["model_path"].is_null());
    CHECK(hit["family"] == "shcs");

    const json miss = match_part_number("NOPE-1", false, Seams{});
    CHECK(miss["matched"] == false);
    CHECK(miss["source"].is_null());
    CHECK(miss["params"].is_null());
    CHECK(miss["model_path"].is_null());
    CHECK(miss["normalized"] == "NOPE-1");

    Seams s;
    s.build_model = [](const json& match) {
        return std::string("/tmp/") + match["catalog_id"].get<std::string>() + ".step";
    };
    const json gen = match_part_number("M3x8", true, s);
    CHECK(gen["model_path"] == "/tmp/ISO 4762 M3x8.step");
}

TEST_CASE("spec_sheet_match: dims lane builds the contract shape offline") {
    const json dims = json::parse(R"({"body_diameter_mm":6.3,"body_length_mm":11.2,
        "lead_spacing_mm":2.5,"rogue_key":1})");
    const json r = spec_sheet_match("", "UKL1E101MED", "capacitor_radial", &dims, Seams{});
    CHECK(r["matched"] == true);
    CHECK(r["source"] == "spec_sheet");
    CHECK(r["mpn"] == "UKL1E101MED");
    CHECK(r["template"] == "capacitor_radial");
    CHECK(r["missing_required"].empty());
    CHECK(r["registered"] == false);  // no build seam -> contract "no model" state
    CHECK(r["vendor_part"].is_null());
    CHECK(r["reason"] == "no editable model produced (native core unavailable offline)");
    CHECK(!r["params"].contains("rogue_key"));  // only template keys survive
    CHECK(r["params"]["body_diameter_mm"] == doctest::Approx(6.3));
    // Deterministic preview (GSL text) — exact bytes.
    CHECK(r["preview"] ==
          "spec-sheet part: capacitor_radial  [UKL1E101MED]\n"
          "template: Radial (can) electrolytic capacitor body\n"
          "  body_diameter_mm = 6.3 mm\n"
          "  body_length_mm = 11.2 mm\n"
          "  lead_spacing_mm = 2.5 mm");
}

TEST_CASE("spec_sheet_match: missing required dims surface, never guessed") {
    const json dims = json::parse(R"({"body_diameter_mm":6.3})");
    const json r = spec_sheet_match("", "UKL1E101MED", "capacitor_radial", &dims, Seams{});
    CHECK(r["matched"] == false);
    CHECK(r["missing_required"] == json::array({"body_length_mm"}));
    CHECK(r["registered"] == false);
}

TEST_CASE("spec_sheet_match: no dims and no extract seam -> contract unknown") {
    const json r = spec_sheet_match("some datasheet text", "MFR-1", "box", nullptr, Seams{});
    CHECK(r["matched"] == false);
    CHECK(r["missing_required"] == json::array({"length_mm", "width_mm", "height_mm"}));
    CHECK(std::string(r["reason"]).find("LLM lane") != std::string::npos);
}

TEST_CASE("template build ops: cylinder/box primitives at exact dims") {
    const json ops = template_build_ops("cylinder", json::parse(R"({"diameter_mm":6.0,"height_mm":9.0})"));
    REQUIRE(ops.size() == 1);
    CHECK(ops[0]["feature"]["type"] == "cylinder");
    CHECK(ops[0]["feature"]["params"]["radius"]["value"] == doctest::Approx(3.0));
    CHECK(ops[0]["feature"]["params"]["height"]["value"] == doctest::Approx(9.0));
    CHECK(ops[0]["feature"]["params"]["radius"]["expr"].is_null());
    const json box = template_build_ops("box", json::parse(R"({"length_mm":1,"width_mm":2,"height_mm":3})"));
    CHECK(box[0]["feature"]["params"]["width"]["value"] == doctest::Approx(2.0));
    CHECK_THROWS(template_build_ops("nope", json::object()));
}

// ---------------------------------------------------------------------------
// Vendor providers (engine_oracle/parts_providers.py) — mock HTTP seam.
// ---------------------------------------------------------------------------

// Queue-based fake transport: each call pops the next canned response; records URLs.
struct FakeHttp {
    std::vector<HttpResult> responses;
    std::vector<std::string> urls;
    std::vector<std::string> bodies;
    HttpResult post(const std::string& url, const HttpHeaders&, const std::string& body) {
        urls.push_back(url);
        bodies.push_back(body);
        REQUIRE(!responses.empty());
        HttpResult r = responses.front();
        responses.erase(responses.begin());
        return r;
    }
};

TEST_CASE("nexar provider maps a GraphQL supply hit into a priced VendorPart") {
    test_setenv("NEXAR_CLIENT_ID", "cid");
    test_setenv("NEXAR_CLIENT_SECRET", "cs");
    auto fake = std::make_shared<FakeHttp>();
    fake->responses.push_back({200, R"({"access_token":"tok123"})", ""});
    fake->responses.push_back({200, R"({"data":{"supSearchMpn":{"results":[{"part":{"mpn":"NE555P","manufacturer":{"name":"Texas Instruments"},"bestDatasheet":{"url":"http://x/ds.pdf"},"sellers":[{"company":{"name":"Digi-Key"},"offers":[{"inventoryLevel":1500,"moq":1,"factoryLeadDays":7,"prices":[{"price":0.55,"quantity":1,"currency":"USD"},{"price":0.42,"quantity":100,"currency":"USD"}]},{"inventoryLevel":0,"prices":[{"price":0.60,"quantity":1}]}]}]}}]}}})", ""});
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    const json rec = provider_fetch("octopart", "NE555P", s);  // alias resolves
    CHECK(rec["supplier"] == "Digi-Key");             // seller name overrides
    CHECK(rec["part_number"] == "NE555P");
    CHECK(rec["description"] == "Texas Instruments NE555P");
    CHECK(rec["product_url"] == "http://x/ds.pdf");
    CHECK(rec["specs"]["manufacturer"] == "Texas Instruments");
    CHECK(rec["source"] == "api");
    REQUIRE(rec["price_breaks"].size() == 2);
    CHECK(rec["price_breaks"][0]["qty"] == 1);
    CHECK(rec["price_breaks"][0]["unit_price"] == doctest::Approx(0.55));
    CHECK(rec["unit_price"] == doctest::Approx(0.55));
    CHECK(rec["currency"] == "USD");
    CHECK(rec["stock"] == 1500);
    CHECK(rec["moq"] == 1);
    CHECK(rec["lead_time_days"] == 7);
    // The token POST hit the identity host first, then the GraphQL API.
    REQUIRE(fake->urls.size() == 2);
    CHECK(fake->urls[0] == "https://identity.nexar.com/connect/token");
    CHECK(fake->urls[1] == "https://api.nexar.com/graphql");
    test_unsetenv("NEXAR_CLIENT_ID");
    test_unsetenv("NEXAR_CLIENT_SECRET");
}

TEST_CASE("mouser provider parses $-prices and stock strings") {
    test_setenv("MOUSER_API_KEY", "mk");
    auto fake = std::make_shared<FakeHttp>();
    fake->responses.push_back({200, R"({"SearchResults":{"Parts":[{
        "Manufacturer":"TI","ManufacturerPartNumber":"LM358","Description":"Op Amp",
        "ProductDetailUrl":"http://m","DataSheetUrl":"http://d","MouserPartNumber":"595-LM358",
        "PriceBreaks":[{"Quantity":1,"Price":"$0.420","Currency":"USD"},
                       {"Quantity":100,"Price":"$0.31","Currency":"USD"}],
        "Availability":"1,234 In Stock","Min":1,"LeadTime":"7 Days"}]}})", ""});
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    const json rec = provider_fetch("mouser", "LM358", s);
    CHECK(rec["supplier"] == "Mouser");
    CHECK(rec["specs"]["mouser_pn"] == "595-LM358");
    REQUIRE(rec["price_breaks"].size() == 2);
    CHECK(rec["price_breaks"][0]["unit_price"] == doctest::Approx(0.42));
    CHECK(rec["unit_price"] == doctest::Approx(0.42));
    CHECK(rec["stock"] == 1234);
    CHECK(rec["moq"] == 1);
    CHECK(rec["lead_time_days"] == 7);
    REQUIRE(fake->urls.size() == 1);
    CHECK(fake->urls[0].find("apiKey=mk") != std::string::npos);
    test_unsetenv("MOUSER_API_KEY");
}

TEST_CASE("digikey provider maps v4 keyword search into a VendorPart") {
    test_setenv("DIGIKEY_CLIENT_ID", "did");
    test_setenv("DIGIKEY_CLIENT_SECRET", "ds");
    auto fake = std::make_shared<FakeHttp>();
    fake->responses.push_back({200, R"({"access_token":"dtok"})", ""});
    fake->responses.push_back({200, R"({"Products":[{
        "Manufacturer":{"Name":"Yageo"},"ManufacturerProductNumber":"RC0603FR-0710KL",
        "ProductDescription":"RES 10K 1%","ProductUrl":"/en/products/detail/x",
        "DigiKeyPartNumber":"311-10.0KFRCT-ND","QuantityAvailable":5000,
        "MinimumOrderQuantity":1,"ManufacturerLeadWeeks":8,
        "ProductVariations":[{"StandardPricing":[
          {"BreakQuantity":1,"UnitPrice":0.10},{"BreakQuantity":100,"UnitPrice":0.05}]}]}]})", ""});
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    const json rec = provider_fetch("digi-key", "RC0603FR-0710KL", s);
    CHECK(rec["supplier"] == "Digi-Key");
    CHECK(rec["part_number"] == "RC0603FR-0710KL");
    CHECK(rec["product_url"] == "https://www.digikey.com/en/products/detail/x");
    CHECK(rec["specs"]["digikey_pn"] == "311-10.0KFRCT-ND");
    CHECK(rec["unit_price"] == doctest::Approx(0.10));
    CHECK(rec["stock"] == 5000);
    CHECK(rec["moq"] == 1);
    CHECK(rec["lead_time_days"] == 56);  // 8 weeks * 7
    test_unsetenv("DIGIKEY_CLIENT_ID");
    test_unsetenv("DIGIKEY_CLIENT_SECRET");
}

TEST_CASE("pricing chain fails closed with no credentials (§0)") {
    // No env keys (defensive: unset in case an earlier case threw before cleanup),
    // empty scratch config: every provider declines -> null (unknown).
    for (const char* v : {"NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET", "DIGIKEY_CLIENT_ID",
                          "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY"})
        test_unsetenv(v);
    CHECK(fetch_price("NE555P", Seams{}).is_null());
    // Even with a transport, keyless providers decline before any network call.
    auto fake = std::make_shared<FakeHttp>();
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    CHECK(fetch_price("NE555P", s).is_null());
    CHECK(fake->urls.empty());  // no network without credentials, ever
}

TEST_CASE("provider registry: unknown supplier + fail-closed scaffolds") {
    CHECK_THROWS_AS(provider_fetch("acme", "123", Seams{}), ProviderError);
    CHECK_THROWS_AS(provider_fetch("grabcad", "123", Seams{}), ProviderError);
    CHECK_THROWS_AS(provider_fetch("mcmaster", "123", Seams{}), ProviderError);
}

// ---------------------------------------------------------------------------
// Quote-out (engine_oracle/quote_providers.py).
// ---------------------------------------------------------------------------

TEST_CASE("jlcpcb signing pins the oracle's known-signature fixture") {
    // engine_oracle/test_quote_providers.py::_FIXTURE_SIG
    CHECK(jlcpcb_sign("POST", "/v1/quote", "1700000000000", "testnonce123",
                      "{\"q\": 5}", "test-secret-key") ==
          "bJKUIiHgLxFFHT1kr/XAGCBHbA/RtWNNl79y7JG5UrM=");
    // Method upper-cased; every field participates.
    const std::string base = jlcpcb_sign("POST", "/p", "1", "n", "b", "k");
    CHECK(jlcpcb_sign("post", "/p", "1", "n", "b", "k") == base);
    CHECK(jlcpcb_sign("POST", "/p", "2", "n", "b", "k") != base);
    CHECK(jlcpcb_sign("POST", "/p", "1", "m", "b", "k") != base);
    CHECK(jlcpcb_sign("POST", "/p", "1", "n", "c", "k") != base);
    CHECK(jlcpcb_sign("POST", "/p", "1", "n", "b", "j") != base);
}

TEST_CASE("request_quote: keyless degrades (§0), never a network call") {
    auto fake = std::make_shared<FakeHttp>();
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    const json r = request_quote("/tmp/part.step", "xometry", 2, "aluminum", 2, "", s);
    CHECK(r["ok"] == false);
    CHECK(r["provider"] == "xometry");
    CHECK(std::string(r["reason"]).find("Xometry API key not configured") != std::string::npos);
    CHECK(fake->urls.empty());

    const json unknown = request_quote("/tmp/part.step", "acme", 1, "", 2, "", s);
    CHECK(unknown["ok"] == false);
    CHECK(std::string(unknown["reason"]).find("unknown quote vendor") != std::string::npos);
}

TEST_CASE("request_quote: xometry hit normalizes into a §4 QuoteResult") {
    test_setenv("XOMETRY_API_KEY", "xk");
    auto fake = std::make_shared<FakeHttp>();
    fake->responses.push_back({200, R"({"unit_price":12.5,"quote_id":"q1","lead_time_days":5})", ""});
    Seams s;
    s.http_post = [fake](const std::string& u, const HttpHeaders& h, const std::string& b) {
        return fake->post(u, h, b);
    };
    const json r = request_quote("/tmp/part.step", "cnc", 3, "aluminum", 2, "", s);
    CHECK(r["ok"] == true);
    CHECK(r["provider"] == "xometry");
    CHECK(r["quantity"] == 3);
    CHECK(r["unit_price"] == doctest::Approx(12.5));
    CHECK(r["ext_price"] == doctest::Approx(37.5));
    CHECK(r["currency"] == "USD");
    CHECK(r["lead_time_days"] == 5);
    CHECK(r["quote_id"] == "q1");
    REQUIRE(fake->urls.size() == 1);
    CHECK(fake->urls[0] == "https://api.xometry.com/v1/quote");
    test_unsetenv("XOMETRY_API_KEY");
}

// ---------------------------------------------------------------------------
// Dispatch envelopes + vendor registry.
// ---------------------------------------------------------------------------

TEST_CASE("dispatch: envelopes mirror the Python worker wrappers") {
    Seams s = fixture_seams();
    s.load_assembly = [](const std::string& path) {
        CHECK(path == "/asm/top.step+");
        return fixture_asm_tree();
    };
    json r;
    CHECK(call("bom_rollup", json::parse(R"({"doc_path":"/asm/top.step+"})"), s, r));
    CHECK(r["ok"] == true);
    CHECK(r["bom"].size() == 4);

    CHECK(call("match_part_number", json::parse(R"({"mpn":"M3x8-ND"})"), s, r));
    CHECK(r["ok"] == true);
    CHECK(r["match"]["matched"] == true);
    CHECK(r["match"]["normalized"] == "M3x8");

    // request_quote keyless: {ok:true, quote:{ok:false,...}} — the §0 quote shape.
    CHECK(call("request_quote", json::parse(R"({"doc_path":"/p.step","vendor":"xometry"})"), s, r));
    CHECK(r["ok"] == true);
    CHECK(r["quote"]["ok"] == false);

    // Loader absent -> the ok:false envelope (Python: exception -> error string).
    CHECK(call("bom_rollup", json::parse(R"({"doc_path":"/asm/top.step+"})"), Seams{}, r));
    CHECK(r["ok"] == false);
    CHECK(r.contains("error"));

    CHECK(call("spec_sheet_match", json::parse(R"({"template":"nope","mpn":"x"})"), s, r));
    CHECK(r["ok"] == false);
    CHECK(std::string(r["error"]).find("unknown template") != std::string::npos);

    CHECK(call("list_spec_templates", json::object(), s, r));
    CHECK(r["templates"].size() == 4);

    CHECK(call("vendor_part_register",
               json::parse(R"({"supplier":"Dispatch Vendor","part_number":"D-1",
                               "unit_price":1.25,"currency":"USD",
                               "provenance":{"method":"browser_scrape","scraped":true}})"),
               s, r));
    CHECK(r["ok"] == true);
    CHECK(r["part"]["unit_price"] == doctest::Approx(1.25));
    CHECK(r["part"]["provenance"]["scraped"] == true);
    CHECK(call("vendor_part_register", json::parse(R"({"part_number":"D-2"})"), s, r));
    CHECK(r["ok"] == false);
    CHECK(r["error"] == "supplier is required");

    CHECK(!call("doc_new", json::object(), s, r));  // not ours -> fall through
}

TEST_CASE("vendor registry: register_local_cad copies + indexes the record") {
    namespace fs = std::filesystem;
    const fs::path root = vendor_parts_dir();
    const fs::path src = fs::temp_directory_path() / "chudware_spec_model_test.step";
    {
        std::ofstream f(src);
        f << "ISO-10303-21;\nEND-ISO-10303-21;\n";
    }
    const json rec = register_local_cad(
        "spec_sheet", "TEST-1",
        {{"cad_path", src.string()},
         {"description", "test part"},
         {"product_url", ""},
         {"specs", json::parse(R"({"body_diameter_mm":6.3})")},
         {"source", "spec_sheet"}});
    CHECK(rec["supplier"] == "spec_sheet");
    CHECK(rec["part_number"] == "TEST-1");
    CHECK(rec["cad_format"] == "step");
    CHECK(rec["source"] == "spec_sheet");
    CHECK(fs::exists(rec["cad_path"].get<std::string>()));
    const fs::path index = root / "index.json";
    REQUIRE(fs::exists(index));
    std::ifstream in(index);
    const json idx = json::parse(in);
    REQUIRE(idx.contains("spec_sheet::test-1"));
    CHECK(idx["spec_sheet::test-1"]["part_number"] == "TEST-1");
    CHECK_THROWS_AS(register_local_cad(
                        "s", "p", {{"cad_path", "/nonexistent/file.step"},
                                   {"source", "manual"}}),
                    ProviderError);
}

TEST_CASE("vendor registry lookup uses supplier-qualified keys and deterministic fallback") {
    namespace fs = std::filesystem;
    register_local_cad("Zeta Supply", "MATCH-42",
                       {{"unit_price", 9.25}, {"currency", "USD"}});
    register_local_cad("Alpha Supply", "MATCH-42",
                       {{"unit_price", 2.50}, {"currency", "USD"}});

    Seams seams;
    seams.vendor_lookup = [](const json& component) -> json {
        try {
            const fs::path index = fs::path(vendor_parts_dir()) / "index.json";
            if (!fs::exists(index)) return json();
            std::ifstream in(index);
            return vendor_index_lookup(json::parse(in), component);
        } catch (...) {
            return json();
        }
    };
    const json tree = json::parse(
        R"({"tree":{"components":[{"mpn":"MATCH-42","vendor_supplier":"Zeta Supply",
                                   "quantity":3}]}})");
    const json bom = bom_rollup_lines(tree, seams);
    REQUIRE(bom.size() == 1);
    REQUIRE(bom[0]["vendor_part"].is_object());
    CHECK(bom[0]["part_ref"] == "zeta-supply::match-42");
    CHECK(bom[0]["vendor_part"]["supplier"] == "Zeta Supply");
    CHECK(bom[0]["vendor_part"]["unit_price"] == doctest::Approx(9.25));

    std::ifstream in(fs::path(vendor_parts_dir()) / "index.json");
    const json index = json::parse(in);
    const json fallback =
        vendor_index_lookup(index, json::parse(R"({"part_number":"MATCH-42"})"));
    CHECK(fallback["supplier"] == "Alpha Supply");
    CHECK(fallback["unit_price"] == doctest::Approx(2.50));
}

TEST_CASE("vendor registry price-only upsert preserves CAD and price provenance") {
    namespace fs = std::filesystem;
    const fs::path src = fs::temp_directory_path() / "chudware_vendor_merge_test.step";
    {
        std::ofstream f(src);
        f << "ISO-10303-21;\nEND-ISO-10303-21;\n";
    }
    const json initial = register_local_cad(
        "Merge Vendor", "MERGE-1",
        {{"cad_path", src.string()},
         {"specs", json::parse(R"({"diameter_mm":12.0})")},
         {"description", "original"}});
    CHECK(initial["fetched_ts"].is_null());

    const json provenance = json::parse(
        R"({"method":"browser_scrape","source_url":"https://example.test/part",
            "session_profile_used":"default","license_note":"source terms apply",
            "scraped":true})");
    const json priced = register_local_cad(
        "Merge Vendor", "MERGE-1",
        {{"unit_price", 4.75}, {"stock", 12}, {"provenance", provenance}});
    CHECK(priced["cad_path"] == initial["cad_path"]);
    CHECK(priced["cad_format"] == "step");
    CHECK(priced["specs"] == initial["specs"]);
    CHECK(priced["added_ts"] == initial["added_ts"]);
    CHECK(priced["unit_price"] == doctest::Approx(4.75));
    REQUIRE(priced["fetched_ts"].is_number());
    CHECK(priced["fetched_ts"].get<double>() > 0.0);
    CHECK(priced["provenance"] == provenance);

    const json fetched_ts = priced["fetched_ts"];
    const json renamed = register_local_cad(
        "Merge Vendor", "MERGE-1", {{"description", "renamed"}});
    CHECK(renamed["fetched_ts"] == fetched_ts);
    CHECK(renamed["cad_path"] == initial["cad_path"]);
    CHECK(renamed["specs"] == initial["specs"]);
    CHECK(renamed["provenance"] == provenance);
    CHECK(renamed["description"] == "renamed");
}

TEST_CASE("vendor registry rejects malformed price breaks at the offending entry") {
    CHECK_THROWS_WITH_AS(
        register_local_cad(
            "Bad Break Vendor", "BAD-1",
            {{"price_breaks",
              json::parse(R"([{"qty":1,"unit_price":0.5},
                               {"qty":"ten","unit_price":0.4}])")}}),
        "price_breaks[1] must be an object with numeric qty and unit_price; got "
        "{\"qty\":\"ten\",\"unit_price\":0.4}",
        ProviderError);
}

int main(int argc, char** argv) {
    // Isolate ALL config/vendor-registry reads to a scratch XDG dir: the providers and
    // the cost model must see an empty user config (no live machine state in tests).
    const std::string scratch =
        (std::filesystem::temp_directory_path() / "chudware_sourcing_test_xdg").string();
    std::error_code ec;
    std::filesystem::remove_all(scratch, ec);
    std::filesystem::create_directories(scratch, ec);
    test_setenv("XDG_DATA_HOME", scratch.c_str());
    // Guard against a developer's live keys leaking into the fail-closed tests.
    for (const char* v :
         {"NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET", "DIGIKEY_CLIENT_ID",
          "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY", "XOMETRY_API_KEY", "JLCPCB_APP_ID",
          "JLCPCB_ACCESS_KEY", "JLCPCB_SECRET_KEY", "COMMODITIES_API_KEY"})
        test_unsetenv(v);
    doctest::Context ctx(argc, argv);
    return ctx.run();
}
