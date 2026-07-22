// core/sourcing/quote.cpp — quote-out adapters (Xometry, JLCPCB). Port of
// engine_oracle/quote_providers.py: approved instant-quote APIs with the customer's
// own credentials (config.json "quote_providers" section or env fallback), §0
// degraded shapes when keyless — never a network call without a key. The JLCPCB
// Open API HMAC-SHA256 request signing (T-BL24) is ported verbatim and pinned
// against the oracle's known-signature fixture. HTTP via the Seams injection
// point; the SHA-256 below is self-contained (no new dependency).
#include "sourcing/sourcing.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cctype>
#include <map>
#include <random>

namespace aicad {
namespace sourcing {

namespace {

// ---- SHA-256 (FIPS 180-4; compact self-contained implementation) -------------------------------

struct Sha256 {
    uint32_t h[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};
    uint8_t buf[64] = {};
    size_t buflen = 0;
    uint64_t total = 0;

    static uint32_t rotr(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

    void block(const uint8_t* p) {
        static const uint32_t k[64] = {
            0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
            0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
            0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
            0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
            0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
            0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
            0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
            0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
            0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
            0xc67178f2};
        uint32_t w[64];
        for (int i = 0; i < 16; ++i)
            w[i] = (uint32_t(p[i * 4]) << 24) | (uint32_t(p[i * 4 + 1]) << 16) |
                   (uint32_t(p[i * 4 + 2]) << 8) | uint32_t(p[i * 4 + 3]);
        for (int i = 16; i < 64; ++i) {
            const uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
            const uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16] + s0 + w[i - 7] + s1;
        }
        uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
        for (int i = 0; i < 64; ++i) {
            const uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
            const uint32_t ch = (e & f) ^ (~e & g);
            const uint32_t t1 = hh + s1 + ch + k[i] + w[i];
            const uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
            const uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
            const uint32_t t2 = s0 + maj;
            hh = g; g = f; f = e; e = d + t1; d = c; c = b; b = a; a = t1 + t2;
        }
        h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
    }

    void update(const uint8_t* data, size_t len) {
        total += len;
        while (len > 0) {
            const size_t take = std::min(len, 64 - buflen);
            std::memcpy(buf + buflen, data, take);
            buflen += take;
            data += take;
            len -= take;
            if (buflen == 64) {
                block(buf);
                buflen = 0;
            }
        }
    }

    void final(uint8_t out[32]) {
        const uint64_t bits = total * 8;
        uint8_t pad = 0x80;
        update(&pad, 1);
        pad = 0;
        while (buflen != 56) update(&pad, 1);
        uint8_t lenb[8];
        for (int i = 0; i < 8; ++i) lenb[i] = uint8_t(bits >> (56 - 8 * i));
        update(lenb, 8);
        for (int i = 0; i < 8; ++i) {
            out[i * 4] = uint8_t(h[i] >> 24);
            out[i * 4 + 1] = uint8_t(h[i] >> 16);
            out[i * 4 + 2] = uint8_t(h[i] >> 8);
            out[i * 4 + 3] = uint8_t(h[i]);
        }
    }
};

void hmac_sha256(const std::string& key, const std::string& msg, uint8_t out[32]) {
    uint8_t k[64] = {};
    if (key.size() > 64) {
        Sha256 s;
        s.update(reinterpret_cast<const uint8_t*>(key.data()), key.size());
        s.final(k);
    } else {
        std::memcpy(k, key.data(), key.size());
    }
    uint8_t ipad[64], opad[64];
    for (int i = 0; i < 64; ++i) {
        ipad[i] = k[i] ^ 0x36;
        opad[i] = k[i] ^ 0x5c;
    }
    Sha256 s1;
    s1.update(ipad, 64);
    s1.update(reinterpret_cast<const uint8_t*>(msg.data()), msg.size());
    uint8_t inner[32];
    s1.final(inner);
    Sha256 s2;
    s2.update(opad, 64);
    s2.update(inner, 32);
    s2.final(out);
}

std::string base64_encode(const uint8_t* data, size_t len) {
    static const char* tbl = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string out;
    for (size_t i = 0; i < len; i += 3) {
        const uint32_t b0 = data[i];
        const uint32_t b1 = i + 1 < len ? data[i + 1] : 0;
        const uint32_t b2 = i + 2 < len ? data[i + 2] : 0;
        const uint32_t v = (b0 << 16) | (b1 << 8) | b2;
        out += tbl[(v >> 18) & 63];
        out += tbl[(v >> 12) & 63];
        out += i + 1 < len ? tbl[(v >> 6) & 63] : '=';
        out += i + 2 < len ? tbl[v & 63] : '=';
    }
    return out;
}

// ---- shared helpers -----------------------------------------------------------------------------

double now_epoch() {
    return std::chrono::duration<double>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

std::string trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : s.substr(b, e - b + 1);
}

std::string host_of(const std::string& url) {
    const auto pos = url.find("://");
    const std::string rest = pos == std::string::npos ? url : url.substr(pos + 3);
    return rest.substr(0, rest.find('/'));
}

std::string path_of(const std::string& url) {
    const auto pos = url.find("://");
    if (pos == std::string::npos) return url;
    const auto slash = url.find('/', pos + 3);
    return slash == std::string::npos ? "/" : url.substr(slash);
}

json provider_config(const std::string& key) {
    const json cfg = load_config();
    const json section = cfg.value("quote_providers", json());
    if (!section.is_object()) return json::object();
    const json sub = section.value(key, json());
    return sub.is_object() ? sub : json::object();
}

std::string resolve_key(const std::string& key, const char* env_name) {
    std::string val;
    const json cfg = provider_config(key);
    if (cfg.contains("api_key") && cfg["api_key"].is_string())
        val = cfg["api_key"].get<std::string>();
    if (val.empty()) {
        const char* env = std::getenv(env_name);
        if (env) val = env;
    }
    return trim(val);
}

json http_post_json(const std::string& url, const json& payload, const HttpHeaders& headers,
                    const Seams& seams) {
    if (!seams.http_post) throw QuoteError("no HTTP transport configured for " + host_of(url));
    HttpHeaders h = {{"Content-Type", "application/json"}, {"Accept", "application/json"}};
    h.insert(h.end(), headers.begin(), headers.end());
    const HttpResult r = seams.http_post(url, h, payload.dump());
    if (r.status == 0)
        throw QuoteError("network error contacting " + host_of(url) + ": " + r.error);
    if (r.status >= 400)
        throw QuoteError("HTTP " + std::to_string(r.status) + " from " + host_of(url));
    if (r.body.find_first_not_of(" \t\r\n") == std::string::npos) return json::object();
    try {
        return json::parse(r.body);
    } catch (...) {
        throw QuoteError("invalid JSON response from " + host_of(url));
    }
}

json degraded(const std::string& provider, const std::string& reason, const std::string& hint) {
    return {{"ok", false}, {"provider", provider}, {"reason", reason}, {"hint", hint}};
}

// Normalize [{"qty","unit_price"}] tiers, ascending.
json price_breaks(const json& tiers) {
    json out = json::array();
    if (tiers.is_array())
        for (const json& t : tiers) {
            if (!t.is_object()) continue;
            const json q = t.value("qty", json());
            const json u = t.value("unit_price", json());
            if (q.is_null() || u.is_null()) continue;
            out.push_back({{"qty", q.get<long long>()}, {"unit_price", u.get<double>()}});
        }
    std::stable_sort(out.begin(), out.end(),
                     [](const json& a, const json& b) { return a["qty"] < b["qty"]; });
    return out;
}

json normalize_result(const std::string& provider, int quantity, const json& raw) {
    if (!raw.is_object())
        return degraded(provider, "empty/invalid quote response",
                        "retry, or check the API status.");
    if (raw.contains("ok") && raw["ok"].is_boolean() && !raw["ok"].get<bool>()) {
        const json reason = raw.value("reason", raw.value("error", json()));
        const json hint = raw.value("hint", json());
        return {{"ok", false},
                {"provider", provider},
                {"reason", reason.is_null() ? "quote declined by provider" : reason},
                {"hint", hint.is_null() ? json("check the request parameters and try again.")
                                        : hint}};
    }

    const json breaks = price_breaks(raw.value("price_breaks", json()));
    json unit_price = raw.value("unit_price", json());
    if (unit_price.is_null()) unit_price = raw.value("unitPrice", json());
    if (unit_price.is_null() && !breaks.empty()) {
        // Highest tier whose qty <= quantity, else the first tier.
        json pick;
        for (const json& b : breaks)
            if (b["qty"].get<long long>() <= quantity) pick = b;
        unit_price = (!pick.is_null() ? pick : breaks[0])["unit_price"];
    }
    if (unit_price.is_null() || !unit_price.is_number())
        return degraded(provider, "quote response carried no unit price",
                        "the provider returned no price; retry or check the part/board is "
                        "quotable.");

    const double up = unit_price.get<double>();
    const json ext = raw.value("ext_price", json());
    const double ext_price = ext.is_number() ? ext.get<double>() : up * quantity;
    const json lead = raw.contains("lead_time_days") ? raw["lead_time_days"]
                                                     : raw.value("leadTimeDays", json());
    // Python `or` chains: a present-but-falsy value (null/""/0) falls through.
    auto or_chain = [&](std::initializer_list<const char*> keys) {
        for (const char* k : keys) {
            if (!raw.contains(k)) continue;
            const json& v = raw[k];
            if (v.is_null()) continue;
            if (v.is_string() && v.get<std::string>().empty()) continue;
            if (v.is_number() && v.get<double>() == 0.0) continue;
            if (v.is_boolean() && !v.get<bool>()) continue;
            return v;
        }
        return json();
    };
    json out;
    out["ok"] = true;
    out["provider"] = provider;
    out["quantity"] = quantity;
    out["unit_price"] = up;
    out["ext_price"] = round6(ext_price);
    const json cur = or_chain({"currency"});
    out["currency"] = cur.is_null() ? json("USD") : cur;
    out["lead_time_days"] = lead.is_number() ? json(lead.get<long long>()) : json();
    out["price_breaks"] = breaks;
    out["quote_id"] = or_chain({"quote_id", "quoteId"});
    out["quote_url"] = or_chain({"quote_url", "quoteUrl"});
    out["expires_ts"] = or_chain({"expires_ts", "expiresTs"});
    out["fetched_ts"] = now_epoch();
    return out;
}

// ---- JLCPCB signing (T-BL24) ----------------------------------------------------------------------

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

std::string uuid4_hex() {
    std::random_device rd;
    std::mt19937_64 gen((uint64_t(rd()) << 32) ^ uint64_t(rd()));
    static const char* hex = "0123456789abcdef";
    std::string out;
    for (int i = 0; i < 32; ++i) out += hex[gen() & 15];
    return out;
}

}  // namespace

std::string jlcpcb_sign(const std::string& method, const std::string& path,
                        const std::string& timestamp, const std::string& nonce,
                        const std::string& body, const std::string& secret_key) {
    const std::string string_to_sign =
        upper(method) + "\n" + path + "\n" + timestamp + "\n" + nonce + "\n" + body;
    uint8_t digest[32];
    hmac_sha256(secret_key, string_to_sign, digest);
    return base64_encode(digest, 32);
}

json request_quote(const std::string& part_file, const std::string& vendor, int quantity,
                   const std::string& material, int layers, const std::string& finish,
                   const Seams& seams) {
    // Vendor resolution (unknown vendor degrades to the §0 shape, never raises).
    std::string key = vendor;
    for (char& c : key) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    key = trim(key);
    static const std::map<std::string, std::string> aliases = {
        {"xom", "xometry"}, {"cnc", "xometry"}, {"mechanical", "xometry"},
        {"jlc", "jlcpcb"}, {"pcb", "jlcpcb"}, {"board", "jlcpcb"},
    };
    const auto alias = aliases.find(key);
    if (alias != aliases.end()) key = alias->second;
    const int qty = quantity >= 1 ? quantity : 1;

    if (key == "xometry") {
        const std::string api_key = resolve_key("xometry", "XOMETRY_API_KEY");
        if (api_key.empty())
            return degraded(
                "xometry",
                "Xometry API key not configured (no quote_providers.xometry.api_key and no "
                "XOMETRY_API_KEY). AI-CAD does not scrape; an approved instant-quote API key "
                "is required to quote out.",
                "add 'quote_providers.xometry.api_key' to " + config_path() +
                    " (or set XOMETRY_API_KEY) then retry request_quote.");
        if (part_file.empty())
            return degraded("xometry", "no STEP file to quote", "export the part to STEP first.");
        const json payload = {{"part_file", part_file},
                              {"process", "cnc_milling"},
                              {"quantity", qty},
                              {"material", material.empty() ? json() : json(material)},
                              {"finish", finish.empty() ? json() : json(finish)},
                              {"options", json::object()}};
        json raw;
        try {
            raw = http_post_json("https://api.xometry.com/v1/quote", payload,
                                 {{"Authorization", "Bearer " + api_key}}, seams);
        } catch (const QuoteError& e) {
            return degraded("xometry", e.what(),
                            "check the Xometry API key and that the part is quotable.");
        }
        return normalize_result("xometry", qty, raw);
    }

    if (key == "jlcpcb") {
        const json cfg = provider_config("jlcpcb");
        auto cred = [&](const char* field, const char* env_name) {
            std::string val;
            if (cfg.contains(field) && cfg[field].is_string())
                val = cfg[field].get<std::string>();
            if (val.empty()) {
                const char* env = std::getenv(env_name);
                if (env) val = env;
            }
            return trim(val);
        };
        const std::string appid = cred("appid", "JLCPCB_APP_ID");
        const std::string accesskey = cred("accesskey", "JLCPCB_ACCESS_KEY");
        const std::string secretkey = cred("secretkey", "JLCPCB_SECRET_KEY");
        if (appid.empty() || accesskey.empty() || secretkey.empty())
            return degraded(
                "jlcpcb",
                "JLCPCB Open API credentials not configured. The Open API requires "
                "per-request HMAC-SHA256 signing with an appid + accesskey + secretkey "
                "triple (the static JLCPCB_KEY bearer is no longer supported). No scraping.",
                "add quote_providers.jlcpcb.{appid,accesskey,secretkey} to " + config_path() +
                    " (or set JLCPCB_APP_ID / JLCPCB_ACCESS_KEY / JLCPCB_SECRET_KEY) then "
                    "retry request_quote.");
        if (part_file.empty())
            return degraded("jlcpcb", "no Gerber bundle to quote",
                            "export the board Gerbers first.");
        const json payload = {{"part_file", part_file},
                              {"process", "pcb_fab"},
                              {"quantity", qty},
                              {"finish", finish.empty() ? json() : json(finish)},
                              {"options", {{"layers", layers}}}};
        const std::string api_url = "https://api.jlcpcb.com/v1/quote";
        const std::string body = payload.dump();
        const std::string ts = std::to_string(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch())
                .count());
        const std::string nonce = uuid4_hex();
        const std::string auth = "JOP appid=\"" + appid + "\", accesskey=\"" + accesskey +
                                 "\", nonce=\"" + nonce + "\", timestamp=\"" + ts +
                                 "\", signature=\"" +
                                 jlcpcb_sign("POST", path_of(api_url), ts, nonce, body,
                                             secretkey) +
                                 "\"";
        json raw;
        try {
            raw = http_post_json(api_url, payload, {{"Authorization", auth}}, seams);
        } catch (const QuoteError& e) {
            return degraded("jlcpcb", e.what(),
                            "check the JLCPCB Open API credentials (appid/accesskey/secretkey), "
                            "the request signing, and the Gerber bundle.");
        }
        return normalize_result("jlcpcb", qty, raw);
    }

    return {{"ok", false},
            {"provider", vendor},
            {"reason", "unknown quote vendor '" + vendor + "'; known: ['jlcpcb', 'xometry']"},
            {"hint", "use one of ['jlcpcb', 'xometry']."}};
}

}  // namespace sourcing
}  // namespace aicad
