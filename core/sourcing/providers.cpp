// core/sourcing/providers.cpp — electronic-distributor pricing providers. Port of
// engine_oracle/parts_providers.py: Nexar (Octopart GraphQL) -> Digi-Key -> Mouser
// chain plus the CAD-fetch provider registry (mcmaster/grabcad/traceparts fail-closed
// scaffolds). No scraping — approved APIs with the customer's own credentials only;
// credentials come from config.json ("parts_providers") or the environment, never
// from code. With no credentials or no transport every provider raises ProviderError
// and the chain degrades to null (the §0 "price unknown" state). The HTTP transport
// is the Seams injection point — tests never touch the network.
#include "sourcing/sourcing.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <map>

namespace chudware {
namespace sourcing {

namespace {

const char* kConfigHint = "See docs/PARTS_LIBRARY.md.";

std::string host_of(const std::string& url) {
    // urlparse(url).netloc
    auto pos = url.find("://");
    std::string rest = pos == std::string::npos ? url : url.substr(pos + 3);
    return rest.substr(0, rest.find('/'));
}

double now_epoch() {
    return std::chrono::duration<double>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string status_reason(long status) {
    static const std::map<long, const char*> phrases = {
        {400, "Bad Request"}, {401, "Unauthorized"}, {403, "Forbidden"},
        {404, "Not Found"}, {429, "Too Many Requests"}, {500, "Internal Server Error"},
        {502, "Bad Gateway"}, {503, "Service Unavailable"}};
    const auto it = phrases.find(status);
    return it != phrases.end() ? it->second : "";
}

// ---- HTTP seam (mirrors the oracle's _http_json helpers; Seams-injected transport) -----------

json parse_response(const HttpResult& r, const std::string& host) {
    if (r.status == 0)
        throw ProviderError("network error contacting " + host + ": " + r.error);
    if (r.status >= 400)
        throw ProviderError("HTTP " + std::to_string(r.status) + " from " + host + ": " +
                            status_reason(r.status));
    if (r.body.find_first_not_of(" \t\r\n") == std::string::npos) return json::object();
    try {
        return json::parse(r.body);
    } catch (...) {
        throw ProviderError("invalid JSON response from " + host);
    }
}

std::string url_encode(const std::string& s) {
    // urllib.parse.urlencode (quote_via=quote_plus) for the ASCII cred/field space.
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : s) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
            c == '_' || c == '.' || c == '-')
            out += (char)c;
        else if (c == ' ')
            out += '+';
        else {
            out += '%';
            out += hex[c >> 4];
            out += hex[c & 15];
        }
    }
    return out;
}

json http_post_form(const std::string& url, const std::map<std::string, std::string>& fields,
                    const HttpHeaders& headers, const Seams& seams) {
    if (!seams.http_post)
        throw ProviderError("no HTTP transport configured for " + host_of(url));
    std::string body;
    for (const auto& kv : fields)
        body += (body.empty() ? "" : "&") + url_encode(kv.first) + "=" + url_encode(kv.second);
    HttpHeaders h = {{"Content-Type", "application/x-www-form-urlencoded"}};
    h.insert(h.end(), headers.begin(), headers.end());
    return parse_response(seams.http_post(url, h, body), host_of(url));
}

json http_post_json(const std::string& url, const json& payload, const HttpHeaders& headers,
                    const Seams& seams) {
    if (!seams.http_post)
        throw ProviderError("no HTTP transport configured for " + host_of(url));
    HttpHeaders h = {{"Content-Type", "application/json"}, {"Accept", "application/json"}};
    h.insert(h.end(), headers.begin(), headers.end());
    return parse_response(seams.http_post(url, h, payload.dump()), host_of(url));
}

// ---- credentials (config.json "parts_providers" section, then env fallback) -------------------

json provider_config(const std::string& key) {
    const json cfg = load_config();
    const json section = cfg.value("parts_providers", json());
    if (!section.is_object()) return json::object();
    const json sub = section.value(key, json());
    return sub.is_object() ? sub : json::object();
}

std::string resolve_cred(const json& cfg, const std::string& field, const char* env_name) {
    std::string val;
    if (cfg.contains(field) && cfg[field].is_string()) val = cfg[field].get<std::string>();
    if (val.empty()) {
        const char* env = std::getenv(env_name);
        if (env) val = env;
    }
    // strip
    const auto b = val.find_first_not_of(" \t\r\n");
    const auto e = val.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : val.substr(b, e - b + 1);
}

// ---- VendorPart record (asdict of engine/vendor_parts.py::VendorPart) -------------------------

json make_vendor_part(const std::string& supplier, const std::string& part_number,
                      const std::string& description, const std::string& product_url,
                      const json& specs) {
    json vp;
    vp["supplier"] = supplier;
    vp["part_number"] = part_number;
    vp["description"] = description;
    vp["cad_path"] = "";
    vp["cad_format"] = "";
    vp["product_url"] = product_url;
    vp["specs"] = specs.is_object() ? specs : json::object();
    vp["source"] = "api";
    vp["added_ts"] = now_epoch();
    vp["source_mtime"] = 0.0;
    vp["unit_price"] = nullptr;
    vp["currency"] = "USD";
    vp["price_breaks"] = json::array();
    vp["stock"] = nullptr;
    vp["lead_time_days"] = nullptr;
    vp["moq"] = nullptr;
    vp["fetched_ts"] = nullptr;
    return vp;
}

std::string jstr(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number_float()) return std::to_string(v.get<double>());
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    return std::string();
}

std::string jstr_trim(const json& v) {
    std::string s = jstr(v);
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : s.substr(b, e - b + 1);
}

json pick(const json& d, std::initializer_list<const char*> keys) {
    if (!d.is_object()) return json();
    for (const char* k : keys)
        if (d.contains(k) && !d[k].is_null()) return d[k];
    return json();
}

std::string trim_copy(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : s.substr(b, e - b + 1);
}

// Normalize an offer's price list into ascending [{"qty","unit_price"}] tiers.
json price_breaks_from(const json& prices, const char* qty_key, const char* price_key) {
    json tiers = json::array();
    if (prices.is_array())
        for (const json& p : prices) {
            if (!p.is_object()) continue;
            const json q = p.value(qty_key, json());
            const json pr = p.value(price_key, json());
            if (q.is_null() || pr.is_null() || !q.is_number() || !pr.is_number()) continue;
            tiers.push_back({{"qty", q.get<long long>()}, {"unit_price", pr.get<double>()}});
        }
    std::stable_sort(tiers.begin(), tiers.end(),
                     [](const json& a, const json& b) { return a["qty"] < b["qty"]; });
    return tiers;
}

// ---- Nexar (Octopart GraphQL) ------------------------------------------------------------------

json nexar_fetch(const std::string& mpn, const Seams& seams) {
    const json cfg = provider_config("nexar");
    const std::string client_id = resolve_cred(cfg, "client_id", "NEXAR_CLIENT_ID");
    const std::string client_secret = resolve_cred(cfg, "client_secret", "NEXAR_CLIENT_SECRET");
    if (client_id.empty() || client_secret.empty())
        throw ProviderError(
            "Nexar provider is not configured. Add 'parts_providers.nexar' with "
            "'client_id' and 'client_secret' to %APPDATA%/Chudware/config.json (or set "
            "NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET). Chudware does NOT scrape Octopart; it "
            "uses the approved Nexar GraphQL API with your own credentials. "
            "See docs/PARTS_LIBRARY.md.");

    const json tok = http_post_form("https://identity.nexar.com/connect/token",
                                    {{"grant_type", "client_credentials"},
                                     {"client_id", client_id},
                                     {"client_secret", client_secret},
                                     {"scope", "supply.domain"}},
                                    {}, seams);
    const std::string token = jstr_trim(tok.value("access_token", json()));
    if (token.empty())
        throw ProviderError("Nexar returned no access token (check client_id/secret)");

    static const char* kQuery =
        "query PriceByMpn($q: String!) {"
        "  supSearchMpn(q: $q, limit: 1) {"
        "    results {"
        "      part {"
        "        mpn manufacturer { name }"
        "        sellers { company { name }"
        "          offers { inventoryLevel moq prices { price quantity currency } factoryLeadDays } }"
        "        bestDatasheet { url }"
        "      }"
        "    }"
        "  }"
        "}";
    const json data = http_post_json("https://api.nexar.com/graphql",
                                     {{"query", kQuery}, {"variables", {{"q", mpn}}}},
                                     {{"Authorization", "Bearer " + token}}, seams);

    const json root = data.value("data", json::object());
    json supply = root.value("supSearchMpn", json());
    if (!supply.is_object()) supply = root.value("supply", json::object());
    const json results = supply.value("results", json::array());
    if (!results.is_array() || results.empty())
        throw ProviderError("Nexar returned no supply results for MPN '" + mpn + "'");
    const json part = results[0].is_object() ? results[0].value("part", json::object())
                                             : json::object();
    const json mfr_obj = pick(part, {"manufacturer"});
    const std::string manufacturer =
        mfr_obj.is_object() ? jstr_trim(mfr_obj.value("name", json())) : std::string();
    const std::string part_number = [&] {
        const json m = part.value("mpn", json());
        return m.is_null() ? mpn : jstr(m);
    }();
    std::string description = trim_copy(manufacturer + " " + part_number);
    if (manufacturer.empty()) description = part_number;
    const json ds = part.value("bestDatasheet", json());
    json specs = json::object();
    if (!manufacturer.empty()) specs["manufacturer"] = manufacturer;
    json vp = make_vendor_part("Nexar", part_number, description,
                               jstr(ds.value("url", json(""))), specs);

    const json sellers = part.value("sellers", json::array());
    if (sellers.is_array() && !sellers.empty()) {
        const json seller = sellers[0];
        const json company = pick(seller, {"company"});
        const std::string seller_name =
            company.is_object() ? jstr_trim(company.value("name", json())) : std::string();
        if (!seller_name.empty()) vp["supplier"] = seller_name;
        // cheapest offer by its minimum listed price
        const json offers = seller.value("offers", json::array());
        const json* best = nullptr;
        double best_price = 0.0;
        if (offers.is_array())
            for (const json& off : offers) {
                double unit = 0.0;
                bool have = false;
                const json prices = off.value("prices", json::array());
                if (prices.is_array())
                    for (const json& p : prices) {
                        const json pr = p.value("price", json());
                        if (!pr.is_number()) continue;
                        if (!have || pr.get<double>() < unit) {
                            unit = pr.get<double>();
                            have = true;
                        }
                    }
                if (!have) continue;
                if (!best || unit < best_price) {
                    best = &off;
                    best_price = unit;
                }
            }
        if (best) {
            json breaks = price_breaks_from((*best).value("prices", json::array()),
                                            "quantity", "price");
            vp["price_breaks"] = breaks;
            if (!breaks.empty()) vp["unit_price"] = breaks[0]["unit_price"];
            const json prices = (*best).value("prices", json::array());
            if (prices.is_array())
                for (const json& p : prices) {
                    const json cur = p.value("currency", json());
                    if (!cur.is_null() && !jstr(cur).empty()) {
                        vp["currency"] = jstr(cur);
                        break;
                    }
                }
            const json inv = (*best).value("inventoryLevel", json());
            if (inv.is_number()) vp["stock"] = inv.get<long long>();
            const json moq = (*best).value("moq", json());
            if (moq.is_number() && moq.get<long long>() != 0) vp["moq"] = moq.get<long long>();
            const json lead = (*best).value("factoryLeadDays", json());
            if (lead.is_number()) vp["lead_time_days"] = lead.get<long long>();
        }
    }
    vp["fetched_ts"] = now_epoch();
    return vp;
}

// ---- Digi-Key (Product Information API v4) ------------------------------------------------------

json digikey_price_breaks(const json& product) {
    json tiers = json::array();
    const json variations = pick(product, {"ProductVariations", "productVariations"});
    if (variations.is_array())
        for (const json& var : variations) {
            if (!var.is_object()) continue;
            const json pricing = pick(var, {"StandardPricing", "standardPricing"});
            if (!pricing.is_array()) continue;
            for (const json& p : pricing) {
                if (!p.is_object()) continue;
                const json qty = pick(p, {"BreakQuantity", "breakQuantity", "Quantity", "quantity"});
                const json price = pick(p, {"UnitPrice", "unitPrice", "Price", "price"});
                if (!qty.is_number() || !price.is_number()) continue;
                tiers.push_back(
                    {{"qty", qty.get<long long>()}, {"unit_price", price.get<double>()}});
            }
        }
    if (tiers.empty()) {
        const json unit = pick(product, {"UnitPrice", "unitPrice"});
        if (unit.is_number())
            tiers.push_back({{"qty", 1}, {"unit_price", unit.get<double>()}});
    }
    // Deduplicate by qty (keep first), sort ascending.
    std::map<long long, double> by_qty;
    for (const json& t : tiers) by_qty.emplace(t["qty"].get<long long>(), t["unit_price"].get<double>());
    json out = json::array();
    for (const auto& kv : by_qty) out.push_back({{"qty", kv.first}, {"unit_price", kv.second}});
    return out;
}

json digikey_fetch(const std::string& mpn, const Seams& seams) {
    const json cfg = provider_config("digikey");
    const std::string client_id = resolve_cred(cfg, "client_id", "DIGIKEY_CLIENT_ID");
    const std::string client_secret = resolve_cred(cfg, "client_secret", "DIGIKEY_CLIENT_SECRET");
    if (client_id.empty() || client_secret.empty())
        throw ProviderError(
            "Digi-Key provider is not configured. Add 'parts_providers.digikey' with "
            "'client_id' and 'client_secret' to %APPDATA%/Chudware/config.json (or set "
            "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET). Chudware does NOT scrape digikey.com; "
            "it uses the approved Product Information API v4. See docs/PARTS_LIBRARY.md.");

    const json tok = http_post_form("https://api.digikey.com/v1/oauth2/token",
                                    {{"grant_type", "client_credentials"},
                                     {"client_id", client_id},
                                     {"client_secret", client_secret}},
                                    {}, seams);
    const std::string token = jstr_trim(tok.value("access_token", json()));
    if (token.empty())
        throw ProviderError("Digi-Key returned no access token (check client_id/secret)");

    const json data = http_post_json(
        "https://api.digikey.com/products/v4/search/keyword",
        {{"Keywords", mpn}, {"RecordCount", 1}, {"RecordStartPosition", 0}},
        {{"Authorization", "Bearer " + token},
         {"X-DIGIKEY-Client-Id", client_id},
         {"X-DIGIKEY-Locale-Site", "US"},
         {"X-DIGIKEY-Locale-Language", "en"},
         {"X-DIGIKEY-Locale-Currency", "USD"}},
        seams);

    const json products = pick(data, {"Products", "products"});
    if (!products.is_array() || products.empty() || !products[0].is_object())
        throw ProviderError("Digi-Key returned no products for MPN '" + mpn + "'");
    const json product = products[0];

    std::string manufacturer;
    const json mfr = pick(product, {"Manufacturer", "manufacturer"});
    if (mfr.is_object()) manufacturer = jstr_trim(pick(mfr, {"Name", "name"}));
    else manufacturer = jstr_trim(mfr);
    const json pn = pick(product, {"ManufacturerProductNumber", "manufacturerProductNumber"});
    const std::string part_number = pn.is_null() ? mpn : jstr(pn);
    std::string description =
        jstr_trim(pick(product, {"ProductDescription", "productDescription",
                                 "DetailedDescription", "detailedDescription"}));
    if (description.empty() && !manufacturer.empty())
        description = trim_copy(manufacturer + " " + part_number);
    std::string product_url = jstr(pick(product, {"ProductUrl", "productUrl", "DetailUrl",
                                                  "detailUrl"}));
    const std::string datasheet =
        jstr(pick(product, {"DatasheetUrl", "datasheetUrl", "DataSheetUrl"}));
    if (!product_url.empty() && product_url[0] == '/')
        product_url = "https://www.digikey.com" + product_url;

    json specs = json::object();
    if (!manufacturer.empty()) specs["manufacturer"] = manufacturer;
    const json dkpn = pick(product, {"DigiKeyPartNumber", "digiKeyPartNumber",
                                     "DigiKeyProductNumber", "digiKeyProductNumber"});
    if (!dkpn.is_null()) specs["digikey_pn"] = jstr(dkpn);
    if (!datasheet.empty()) specs["datasheet_url"] = datasheet;

    json vp = make_vendor_part("Digi-Key", part_number, description,
                               product_url.empty() ? datasheet : product_url, specs);
    json breaks = digikey_price_breaks(product);
    vp["price_breaks"] = breaks;
    if (!breaks.empty())
        vp["unit_price"] = breaks[0]["unit_price"];
    else {
        const json unit = pick(product, {"UnitPrice", "unitPrice"});
        if (unit.is_number()) vp["unit_price"] = unit.get<double>();
    }

    json stock = pick(product, {"QuantityAvailable", "quantityAvailable"});
    if (stock.is_null()) {
        const json variations = pick(product, {"ProductVariations", "productVariations"});
        if (variations.is_array() && !variations.empty() && variations[0].is_object())
            stock = pick(variations[0], {"QuantityAvailableforPackageType",
                                         "quantityAvailableforPackageType", "QuantityAvailable",
                                         "quantityAvailable"});
    }
    if (stock.is_number()) vp["stock"] = stock.get<long long>();

    json moq = pick(product, {"MinimumOrderQuantity", "minimumOrderQuantity"});
    if (moq.is_null()) {
        const json variations = pick(product, {"ProductVariations", "productVariations"});
        if (variations.is_array() && !variations.empty() && variations[0].is_object())
            moq = pick(variations[0], {"MinimumOrderQuantity", "minimumOrderQuantity"});
    }
    if (moq.is_number()) vp["moq"] = moq.get<long long>();

    const json lead = pick(product, {"ManufacturerLeadWeeks", "manufacturerLeadWeeks",
                                     "StandardLeadTime", "standardLeadTime"});
    if (!lead.is_null()) {
        if (lead.is_number()) {
            vp["lead_time_days"] = static_cast<long long>(lead.get<double>() * 7);
        } else if (lead.is_string()) {
            std::string num;
            for (char c : lead.get<std::string>()) {
                if (std::isdigit(static_cast<unsigned char>(c)) || c == '.')
                    num += c;
                else if (!num.empty())
                    break;
            }
            if (!num.empty()) {
                try {
                    const double weeks = std::stod(num);
                    vp["lead_time_days"] =
                        weeks > 52 ? static_cast<long long>(weeks)
                                   : static_cast<long long>(weeks * 7);
                } catch (...) {
                }
            }
        }
    }
    vp["currency"] = "USD";
    vp["fetched_ts"] = now_epoch();
    return vp;
}

// ---- Mouser (Search API v2) ---------------------------------------------------------------------

json mouser_fetch(const std::string& mpn, const Seams& seams) {
    const json cfg = provider_config("mouser");
    const std::string api_key = resolve_cred(cfg, "api_key", "MOUSER_API_KEY");
    if (api_key.empty())
        throw ProviderError(
            "Mouser provider is not configured. Add 'parts_providers.mouser' with "
            "'api_key' to %APPDATA%/Chudware/config.json (or set MOUSER_API_KEY). Chudware does "
            "NOT scrape mouser.com; it uses the approved Search API v2. "
            "See docs/PARTS_LIBRARY.md.");

    const json data = http_post_json(
        "https://api.mouser.com/api/v2/search/partnumber?apiKey=" + url_encode(api_key),
        {{"SearchByPartRequest", {{"mouserPartNumber", mpn}, {"partSearchOptions", "None"}}}},
        {}, seams);

    json root = data.value("SearchResults", json());
    if (!root.is_object()) root = data.value("searchResults", json::object());
    json parts = root.value("Parts", json());
    if (!parts.is_array()) parts = root.value("parts", json::array());
    if (!parts.is_array() || parts.empty()) {
        json errors = data.value("Errors", json());
        if (!errors.is_array()) errors = data.value("errors", json());
        if (!errors.is_array()) errors = root.value("Errors", json::array());
        if (errors.is_array() && !errors.empty()) {
            std::string msg;
            for (const json& e : errors) {
                if (e.is_null()) continue;
                const std::string m =
                    e.is_object() ? jstr(e.value("Message", e.value("message", json()))) : jstr(e);
                msg += (msg.empty() ? "" : "; ") + m;
            }
            throw ProviderError("Mouser API error for MPN '" + mpn + "': " + msg);
        }
        throw ProviderError("Mouser returned no parts for MPN '" + mpn + "'");
    }
    if (!parts[0].is_object()) throw ProviderError("Mouser returned no parts for MPN '" + mpn + "'");
    const json part = parts[0];

    const std::string manufacturer =
        jstr_trim(part.value("Manufacturer", part.value("manufacturer", json())));
    const json pn = part.value("ManufacturerPartNumber",
                               part.value("manufacturerPartNumber", json()));
    const std::string part_number = pn.is_null() ? mpn : jstr(pn);
    std::string description =
        jstr_trim(part.value("Description", part.value("description", json())));
    if (description.empty() && !manufacturer.empty())
        description = trim_copy(manufacturer + " " + part_number);
    const std::string product_url =
        jstr(part.value("ProductDetailUrl", part.value("productDetailUrl", json())));
    const std::string datasheet =
        jstr(part.value("DataSheetUrl", part.value("dataSheetUrl", json())));

    json specs = json::object();
    if (!manufacturer.empty()) specs["manufacturer"] = manufacturer;
    const json mpn2 = part.value("MouserPartNumber", part.value("mouserPartNumber", json()));
    if (!mpn2.is_null()) specs["mouser_pn"] = jstr(mpn2);
    if (!datasheet.empty()) specs["datasheet_url"] = datasheet;

    json vp = make_vendor_part("Mouser", part_number, description,
                               product_url.empty() ? datasheet : product_url, specs);

    // PriceBreaks: [{Quantity, Price, Currency}, ...] — Price is often "$0.420".
    json tiers = json::array();
    std::string currency;
    const json raw_breaks = part.value("PriceBreaks", part.value("priceBreaks", json()));
    if (raw_breaks.is_array())
        for (const json& pb : raw_breaks) {
            if (!pb.is_object()) continue;
            const json qty = pb.contains("Quantity") ? pb["Quantity"] : pb.value("quantity", json());
            const json price_raw = pb.contains("Price") ? pb["Price"] : pb.value("price", json());
            if (qty.is_null() || price_raw.is_null()) continue;
            std::string price_s = jstr(price_raw);
            std::string cleaned;
            for (char c : price_s)
                if (c != '$' && c != ',') cleaned += c;
            cleaned = trim_copy(cleaned);
            try {
                tiers.push_back({{"qty", qty.get<long long>()},
                                 {"unit_price", std::stod(cleaned)}});
            } catch (...) {
                continue;
            }
            const json cur = pb.contains("Currency") ? pb["Currency"] : pb.value("currency", json());
            if (!cur.is_null() && currency.empty()) currency = jstr(cur);
        }
    std::stable_sort(tiers.begin(), tiers.end(),
                     [](const json& a, const json& b) { return a["qty"] < b["qty"]; });
    vp["price_breaks"] = tiers;
    if (!tiers.empty()) vp["unit_price"] = tiers[0]["unit_price"];
    if (!currency.empty()) vp["currency"] = currency;

    // stock: FactoryStock numeric, else leading integer of "1,234 In Stock".
    const json factory = part.contains("FactoryStock") ? part["FactoryStock"]
                                                       : part.value("factoryStock", json());
    if (!factory.is_null()) {
        std::string s = jstr(factory);
        s.erase(std::remove(s.begin(), s.end(), ','), s.end());
        try {
            vp["stock"] = std::stoll(trim_copy(s));
        } catch (...) {
        }
    } else {
        const json avail = part.value("Availability", part.value("availability", json()));
        if (!avail.is_null()) {
            std::string num;
            for (char c : jstr_trim(avail)) {
                if (std::isdigit(static_cast<unsigned char>(c)))
                    num += c;
                else if ((c == ',' || c == '.') && !num.empty())
                    continue;
                else if (!num.empty())
                    break;
            }
            if (!num.empty()) {
                try { vp["stock"] = std::stoll(num); } catch (...) {}
            }
        }
    }

    const json moq = part.contains("Min") ? part["Min"] : part.value("min", json());
    if (moq.is_number()) vp["moq"] = moq.get<long long>();

    const json lead = part.contains("LeadTime") ? part["LeadTime"] : part.value("leadTime", json());
    if (!lead.is_null()) {
        if (lead.is_number()) {
            vp["lead_time_days"] = lead.get<long long>();
        } else if (lead.is_string()) {
            const std::string s = jstr_trim(lead);
            std::string low = s;
            std::transform(low.begin(), low.end(), low.begin(),
                           [](unsigned char c) { return (char)std::tolower(c); });
            std::string num;
            for (char c : s) {
                if (std::isdigit(static_cast<unsigned char>(c)) || c == '.')
                    num += c;
                else if (!num.empty())
                    break;
            }
            if (!num.empty()) {
                try {
                    const double val = std::stod(num);
                    long long days;
                    if (low.find("week") != std::string::npos)
                        days = static_cast<long long>(val * 7);
                    else
                        days = static_cast<long long>(val);  // "day" or bare number: days
                    vp["lead_time_days"] = days;
                } catch (...) {
                }
            }
        }
    }
    vp["fetched_ts"] = now_epoch();
    return vp;
}

}  // namespace

json fetch_price(const std::string& mpn, const Seams& seams) {
    // Nexar -> Digi-Key -> Mouser; each ProviderError falls through (§0 degradation).
    for (const auto& fetch : {nexar_fetch, digikey_fetch, mouser_fetch}) {
        try {
            return fetch(mpn, seams);
        } catch (const ProviderError&) {
            continue;
        }
    }
    return json();
}

json provider_fetch(const std::string& supplier, const std::string& part_number,
                    const Seams& seams) {
    static const std::map<std::string, std::string> aliases = {
        {"mcmaster", "mcmaster"}, {"mcmaster-carr", "mcmaster"}, {"mcmaster carr", "mcmaster"},
        {"grabcad", "grabcad"}, {"grab cad", "grabcad"},
        {"traceparts", "traceparts"}, {"trace parts", "traceparts"},
        {"nexar", "nexar"}, {"octopart", "nexar"},
        {"digikey", "digikey"}, {"digi-key", "digikey"}, {"digi key", "digikey"},
        {"mouser", "mouser"},
    };
    std::string s = supplier;
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    s = trim_copy(s);
    const auto alias = aliases.find(s);
    const std::string key = alias != aliases.end() ? alias->second : s;
    const std::string mpn = trim_copy(part_number);

    if (key == "nexar") {
        if (mpn.empty()) throw ProviderError("a manufacturer part number (MPN) is required");
        return nexar_fetch(mpn, seams);
    }
    if (key == "digikey") {
        if (mpn.empty()) throw ProviderError("a manufacturer part number (MPN) is required");
        return digikey_fetch(mpn, seams);
    }
    if (key == "mouser") {
        if (mpn.empty()) throw ProviderError("a manufacturer part number (MPN) is required");
        return mouser_fetch(mpn, seams);
    }
    if (key == "mcmaster") {
        if (mpn.empty()) throw ProviderError("a McMaster-Carr part number is required");
        const json cfg = provider_config("mcmaster");
        const bool enabled = cfg.value("enabled", false);
        const bool have_creds = cfg.contains("api_url") && cfg.contains("username") &&
                                (cfg.contains("password") || cfg.contains("client_cert"));
        if (!enabled || !have_creds)
            throw ProviderError(
                "McMaster-Carr provider is not configured. McMaster requires an approved "
                "Product Information API account with a client certificate and login "
                "credentials. Chudware does NOT scrape mcmaster.com. To enable it, add a "
                "'parts_providers.mcmaster' section to %APPDATA%/Chudware/config.json with "
                "'enabled': true, 'api_url', 'username', 'password', and 'client_cert' "
                "(see docs/PARTS_LIBRARY.md). Until then, download the CAD for '" + mpn +
                "' from mcmaster.com and register it with vendor_part_register.");
        throw ProviderError(
            "McMaster-Carr API adapter is not wired yet. Credentials are configured, but the "
            "approved Product Information API call must be implemented before automated fetch "
            "can run. Download the CAD manually and register it with "
            "vendor_part_register, or wait for the API adapter.");
    }
    if (key == "grabcad" || key == "traceparts") {
        const std::string display = key == "grabcad" ? "GrabCAD" : "TraceParts";
        throw ProviderError(
            display + " provider is not yet configured. No website scraping is performed. "
            "Download the CAD for '" + mpn + "' from " + display +
            " and register it with vendor_part_register, or wire an approved API "
            "adapter. See docs/PARTS_LIBRARY.md.");
    }
    throw ProviderError(
        "no provider for supplier '" + supplier +
        "'. Known providers: digikey, grabcad, mcmaster, mouser, nexar, traceparts. "
        "For unsupported suppliers, register a downloaded CAD file with "
        "vendor_part_register.");
}

}  // namespace sourcing
}  // namespace chudware
