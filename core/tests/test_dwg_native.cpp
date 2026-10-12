// Native DWG ACIS reader (core/doc/dwg_native.*).
//
// The suite builds complete synthetic drawings in memory, so every consumer-visible rule is
// pinned without shipping a customer DWG:
//   * AC1032: encrypted R2004 header, page map, section map, masked + checksummed pages,
//     R2010+ objects, classes with a string stream and an AcDs data store. Each ACIS body is
//     matched to its entity by the data-store record handle (never by order), including
//     records spilled into blob01 pages and stale records left by earlier saves;
//   * AC1015: flat file with section locators, R2000 objects (previous/next links before the
//     layer), inline SAT whose blocks split a token and a CR LF pair, object CRC checks;
//   * AC1018: R2004 objects with an inline ENC colour name, inline SAB ending in the
//     one-identifier End-of-ACIS-data marker, and inline SAT text as version 2;
//   * owner = explicit owner handle, or the *Model_Space / *Paper_Space block record when the
//     entity mode makes it implicit; layer comes after colour/owner/reactor/xdictionary handles;
//   * pre-R13 drawings return an empty list; corrupt input throws a named reason.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include "doc/dwg_native.h"

using namespace chudware::doc;
namespace fs = std::filesystem;

namespace {

// ---- little-endian byte building ----------------------------------------------------------

void put16(std::string& s, std::uint32_t v) {
    s += char(v & 0xFF);
    s += char(v >> 8 & 0xFF);
}
void put32(std::string& s, std::uint32_t v) {
    for (int i = 0; i < 4; ++i) s += char(v >> (8 * i) & 0xFF);
}
void put64(std::string& s, std::uint64_t v) {
    for (int i = 0; i < 8; ++i) s += char(v >> (8 * i) & 0xFF);
}
void set32(std::string& s, std::size_t at, std::uint32_t v) {
    for (int i = 0; i < 4; ++i) s[at + i] = char(v >> (8 * i) & 0xFF);
}
void pad_to(std::string& s, std::size_t n, char fill) {
    if (s.size() < n) s.append(n - s.size(), fill);
}

// ---- MSB-first bit writer (spec §2) ---------------------------------------------------------

struct Bits {
    std::vector<bool> v;
    void b(unsigned x) { v.push_back(x & 1); }
    void bits(std::uint64_t x, int n) {
        for (int i = n - 1; i >= 0; --i) b(static_cast<unsigned>(x >> i & 1));
    }
    void bb(unsigned x) { bits(x, 2); }
    void rc(unsigned x) { bits(x & 0xFF, 8); }
    void rs(unsigned x) { rc(x); rc(x >> 8); }
    void rl(std::uint32_t x) { rs(x & 0xFFFF); rs(x >> 16); }
    void bs(unsigned x) {
        if (x == 0) bb(2);
        else if (x == 256) bb(3);
        else if (x < 256) { bb(1); rc(x); }
        else { bb(0); rs(x); }
    }
    void bl(std::uint32_t x) {
        if (x == 0) bb(2);
        else if (x < 256) { bb(1); rc(x); }
        else { bb(0); rl(x); }
    }
    void bd_one() { bb(1); }
    void ot(unsigned t) {
        if (t < 256) { bb(0); rc(t); }
        else if (t >= 0x1F0 && t < 0x2F0) { bb(1); rc(t - 0x1F0); }
        else { bb(2); rs(t); }
    }
    void h(unsigned code, std::uint64_t value) {
        unsigned n = 0;
        for (std::uint64_t t = value; t; t >>= 8) ++n;
        rc(code << 4 | n);
        for (int i = static_cast<int>(n) - 1; i >= 0; --i) rc(static_cast<unsigned>(value >> (8 * i)));
    }
    void tu(const std::string& s) {
        bs(static_cast<unsigned>(s.size()));
        for (char c : s) rs(static_cast<unsigned char>(c));
    }
    void tv(const std::string& s) {
        bs(static_cast<unsigned>(s.size()));
        bytes(s);
    }
    void bytes(const std::string& s) {
        for (char c : s) rc(static_cast<unsigned char>(c));
    }
    void append(const Bits& o) { v.insert(v.end(), o.v.begin(), o.v.end()); }
    std::string to_bytes() const {
        std::string out((v.size() + 7) / 8, '\0');
        for (std::size_t i = 0; i < v.size(); ++i)
            if (v[i]) out[i / 8] = char(static_cast<unsigned char>(out[i / 8]) | 0x80 >> (i % 8));
        return out;
    }
};

std::string umc(std::uint64_t v) {
    std::string s;
    while (v >= 0x80) { s += char((v & 0x7F) | 0x80); v >>= 7; }
    s += char(v);
    return s;
}
std::string smc_positive(std::uint64_t v) {
    std::string s;
    while (v >= 0x40) { s += char((v & 0x7F) | 0x80); v >>= 7; }
    s += char(v);
    return s;
}

constexpr int kR2000 = 1015;
constexpr int kR2004 = 1018;
constexpr int kR2007 = 1021;
constexpr int kR2010 = 1024;
constexpr int kR2013 = 1027;
constexpr int kR2018 = 1032;

// §2.14.1 object CRC, written from the spec's table definition (CRC-16, polynomial 0xA001).
std::uint16_t crc16(std::uint16_t crc, const std::string& d) {
    for (const char ch : d) {
        unsigned c = (crc ^ static_cast<unsigned char>(ch)) & 0xFF;
        for (int k = 0; k < 8; ++k) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
        crc = static_cast<std::uint16_t>((crc >> 8) ^ c);
    }
    return crc;
}

// Object record framed for `ver`: MS size, [R2010+ MC handle-stream bits], type (R2010+ OT,
// else BS + [R2000+] RL data bit size), `body` (own handle onwards), [R2007+ string stream +
// flag], handle stream, CRC. Pre-R2007 callers write TV strings inline in `body`.
std::string object_record(int ver, unsigned type, const Bits& body,
                          const std::vector<std::string>& strings, const Bits& handles) {
    Bits data = body;
    if (ver >= kR2007) {
        if (strings.empty()) {
            data.b(0);
        } else {
            Bits str;
            for (const auto& s : strings) str.tu(s);
            data.append(str);
            data.rs(static_cast<unsigned>(str.v.size()));
            data.b(1);
        }
    }
    Bits all;
    if (ver >= kR2010) {
        all.ot(type);
    } else {
        all.bs(type);
        const std::size_t bits = all.v.size() + 32 + data.v.size();
        all.rl(static_cast<std::uint32_t>(bits));
    }
    all.append(data);
    const std::size_t data_bits = all.v.size();
    all.append(handles);
    const std::string bytes = all.to_bytes();
    std::string rec;
    put16(rec, static_cast<std::uint32_t>(bytes.size()));  // MS (< 0x8000)
    if (ver >= kR2010) rec += umc(bytes.size() * 8 - data_bits);
    rec += bytes;
    put16(rec, crc16(0xC0C1, rec));
    return rec;
}

Bits object_head(std::uint64_t handle) {
    Bits m;
    m.h(0, handle);
    m.bs(0);  // no EED
    return m;
}

// Non-entity common data: no reactors, xdictionary missing (R2004+; earlier versions always
// store one, see xdic_handle), no data-store data (R2013+).
Bits plain_object(int ver, std::uint64_t handle) {
    Bits m = object_head(handle);
    m.bl(0);
    if (ver >= kR2004) m.b(1);
    if (ver >= kR2013) m.b(0);
    return m;
}
void xdic_handle(int ver, Bits& h) {
    if (ver < kR2004) h.h(3, 0);
}

// A table record whose first field is its name (BLOCK_HEADER 49, LAYER 51).
std::string named_record(int ver, unsigned type, std::uint64_t handle, std::uint64_t owner,
                         const std::string& name) {
    Bits body = plain_object(ver, handle);
    if (ver < kR2007) body.tv(name);
    Bits h;
    h.h(4, owner);
    xdic_handle(ver, h);
    return object_record(ver, type, body, ver < kR2007 ? std::vector<std::string>{}
                                                      : std::vector<std::string>{name}, h);
}

std::string block_control(int ver, std::uint64_t ms, std::uint64_t ps, std::uint64_t other) {
    Bits m = plain_object(ver, 1);
    m.bl(1);  // one named block besides the spaces
    Bits h;
    h.h(4, 0);
    xdic_handle(ver, h);
    h.h(2, other);
    h.h(3, ms);
    h.h(3, ps);
    return object_record(ver, 48, m, {}, h);
}

struct EntitySpec {
    unsigned type = 38;
    std::uint64_t handle = 0;
    unsigned entmode = 2;
    std::uint64_t owner = 0;     // entmode 0 only
    unsigned reactors = 0;
    bool xdic = false;           // R2004+; earlier versions always store one
    bool links = false;          // R2000: previous/next entity handles precede the layer
    bool dbcolor = false;        // ENC with an AcDbColor handle (handle stream, first)
    bool color_name = false;     // ENC colour name (R2004: inline TV)
    bool has_ds = true;          // R2013+
    bool acis_empty = false;
    std::uint64_t layer = 0x10;
    std::vector<std::string> sat_blocks;  // AcDbModelerGeometry version 1, already encrypted
    std::string inline_v2;                // AcDbModelerGeometry version 2 payload
};

EntitySpec spec(unsigned type, std::uint64_t handle) {
    EntitySpec e;
    e.type = type;
    e.handle = handle;
    return e;
}

std::string entity_record(int ver, const EntitySpec& e) {
    Bits m = object_head(e.handle);
    m.b(0);  // no graphic
    m.bb(e.entmode);
    m.bl(e.reactors);
    if (ver >= kR2004) m.b(e.xdic ? 0 : 1);
    if (ver <= kR2000) m.b(e.links ? 0 : 1);
    if (ver >= kR2013) m.b(e.has_ds ? 1 : 0);
    if (ver >= kR2004 && e.dbcolor) {
        // flags 0x40 (handle) | 0x80, plus 0x01 when a colour name follows; no RGB BL
        m.bs((0xC0u | (e.color_name ? 0x01u : 0u)) << 8 | 5);
        if (e.color_name && ver < kR2007) m.tv("Steel Blue");
    } else {
        m.bs(256);  // BYLAYER (CMC index before R2004)
    }
    m.bd_one();  // linetype scale
    if (ver >= kR2000) { m.bb(0); m.bb(0); }
    if (ver >= kR2007) { m.bb(0); m.rc(0); }
    if (ver >= kR2010) { m.b(0); m.b(0); m.b(0); }
    m.bs(0);
    if (ver >= kR2000) m.rc(29);
    m.b(e.acis_empty ? 1 : 0);
    if (!e.acis_empty) {
        m.b(0);
        if (!e.sat_blocks.empty()) {
            m.bs(1);
            for (const auto& block : e.sat_blocks) {
                m.bl(static_cast<std::uint32_t>(block.size()));
                m.bytes(block);
            }
            m.bl(0);
        } else {
            m.bs(2);
            m.bytes(e.inline_v2);
        }
    }
    Bits h;
    if (ver >= kR2004 && e.dbcolor) h.h(5, 0x77);
    if (e.entmode == 0) h.h(4, e.owner);
    for (unsigned i = 0; i < e.reactors; ++i) h.h(4, 0x50 + i);
    if (ver < kR2004 || e.xdic) h.h(3, 0x60);
    if (ver == kR2000 && e.links) { h.h(4, 0x70); h.h(4, 0x71); }
    h.h(5, e.layer);
    return object_record(ver, e.type, m, {}, h);
}

// Object map (§23): blocks of at most `per_block` entries; handle and offset deltas restart
// at zero in every block.
std::string object_map(const std::vector<std::pair<std::uint64_t, std::uint64_t>>& map,
                       std::size_t per_block) {
    std::string out;
    for (std::size_t i = 0; i < map.size(); i += per_block) {
        std::string block;
        std::uint64_t last_h = 0, last_off = 0;
        for (std::size_t k = i; k < std::min(map.size(), i + per_block); ++k) {
            block += umc(map[k].first - last_h) + smc_positive(map[k].second - last_off);
            last_h = map[k].first;
            last_off = map[k].second;
        }
        out += char((block.size() + 2) >> 8);
        out += char((block.size() + 2) & 0xFF);
        out += block + std::string(2, '\0');
    }
    return out + std::string("\x00\x02\x00\x00", 4);
}

const std::string kClassSentinel("\x8D\xA1\xC4\xB8\xC4\xA9\xF8\xC5\xC0\xDC\xF4\x5F\xE7\xCF\xB6\x8A", 16);

// AcDb:Classes with one entity class `dxf` numbered 500, laid out for `ver`.
std::string classes_section(int ver, const std::string& dxf) {
    std::string s = kClassSentinel;
    if (ver < kR2007) {
        Bits c;
        if (ver >= kR2004) { c.bs(500); c.rc(0); c.rc(0); c.b(1); }
        c.bs(500); c.bs(0x47F);
        c.tv("ObjectDBX Classes"); c.tv("AcDbSurface"); c.tv(dxf);
        c.b(0); c.bs(0x1F2);
        if (ver >= kR2004) { c.bl(1); c.bs(25); c.bs(0); c.bl(0); c.bl(0); }
        const std::string bytes = c.to_bytes();
        put32(s, static_cast<std::uint32_t>(bytes.size()));
        return s + bytes + std::string(2, '\0');
    }
    put32(s, 0);                     // size (unused by the reader)
    if (ver >= kR2018) put32(s, 0);  // hsize
    Bits main, str;
    main.bs(500); main.rc(0); main.rc(0); main.b(1);
    main.bs(500); main.bs(0x47F);
    str.tu("ObjectDBX Classes"); str.tu("AcDbSurface"); str.tu(dxf);
    main.b(0); main.bs(0x1F2); main.bl(1); main.bl(31); main.bl(0); main.bl(0); main.bl(0);
    Bits all = main;
    all.append(str);
    all.rs(static_cast<unsigned>(str.v.size()));
    all.b(1);
    put32(s, static_cast<std::uint32_t>(32 + all.v.size()));
    return s + all.to_bytes() + std::string(2, '\0');
}

// ---- AcDs data store (spec chapter 24) ------------------------------------------------------

std::string seg_header(const char* name, std::uint32_t idx, std::uint32_t size, std::uint32_t sysalign,
                       std::uint32_t objalign, bool blob) {
    std::string s;
    put16(s, 0xD5AC);
    s.append(name, 6);
    put32(s, idx);
    put32(s, blob ? 1 : 0);
    put32(s, size);
    put32(s, 0);
    put32(s, 28);
    put32(s, 0);
    put32(s, sysalign);
    put32(s, objalign);
    s.append(8, '\x55');
    return s;
}

struct AcdsRecordSpec {
    std::uint64_t handle;
    std::uint32_t schema;  // 0 thumbnail, 1 ASM
    std::string data;
    bool blob = false;     // spill into two blob01 pages
};

std::string build_acds(const std::vector<AcdsRecordSpec>& recs, const std::string& stale_sab) {
    // Segment numbers: 1 segidx, 2 datidx, 3 schidx, 4 schdat, 5 _data_, 6/7 blob01 pages.
    std::map<std::uint32_t, std::string> segs;
    std::uint32_t asm_schema_offset = 0;

    // schdat: two schemas, property names at the system data offset.
    {
        std::string body;
        auto schema = [&](std::uint32_t payload_name) {
            put16(body, 0);  // no indexes
            put16(body, 2);
            put32(body, 0); put32(body, 0); put32(body, 10); put16(body, 0);            // AcDbDs::ID
            put32(body, 0); put32(body, payload_name); put32(body, 15); put16(body, 0); // payload
        };
        schema(2);  // offset 0: thumbnail
        asm_schema_offset = static_cast<std::uint32_t>(body.size());
        schema(1);  // ASM
        std::string names;
        put32(names, 3);
        names += std::string("AcDbDs::ID") + '\0' + "ASM_Data" + '\0' + "Thumbnail_Data" + '\0';
        const std::size_t name_at = (0x30 + body.size() + 15) / 16 * 16;
        std::string seg = seg_header("schdat", 4, 0, static_cast<std::uint32_t>(name_at / 16), 0, false);
        seg += body;
        pad_to(seg, name_at, 0);
        seg += names;
        pad_to(seg, (seg.size() + 0x7F) / 0x80 * 0x80, 0x70);
        set32(seg, 0x10, static_cast<std::uint32_t>(seg.size()));
        segs[4] = seg;
    }
    // schidx
    {
        std::string body;
        put32(body, 2); put32(body, 0);
        put32(body, 0); put32(body, 4); put32(body, 0);   // schema 0 at schdat+0
        put32(body, 1); put32(body, 4); put32(body, asm_schema_offset);  // schema 1
        put64(body, 0x0AF10C);
        put32(body, 0); put32(body, 0);
        put32(body, 2);
        body += std::string("AcDb_Thumbnail_Schema") + '\0' + "AcDb3DSolid_ASM_Data" + '\0';
        std::string seg = seg_header("schidx", 3, 0, 0, 0, false) + body;
        pad_to(seg, (seg.size() + 0x7F) / 0x80 * 0x80, 0x70);
        set32(seg, 0x10, static_cast<std::uint32_t>(seg.size()));
        segs[3] = seg;
    }
    // _data_: a stale header first (not in the data index), then the live ones.
    std::vector<std::pair<std::uint32_t, std::uint32_t>> datidx;  // local header offset, schema
    {
        std::string headers, data;
        auto add = [&](std::uint64_t handle, const std::string& payload) {
            const auto local = static_cast<std::uint32_t>(headers.size());
            put32(headers, 20); put32(headers, 1); put64(headers, handle);
            put32(headers, static_cast<std::uint32_t>(data.size()));
            data += payload;
            return local;
        };
        auto inline_rec = [](const std::string& bytes) {
            std::string r;
            put32(r, static_cast<std::uint32_t>(bytes.size()));
            return r + bytes;
        };
        add(0x316, inline_rec(stale_sab));
        for (const auto& rec : recs) {
            std::string payload;
            if (!rec.blob) {
                payload = inline_rec(rec.data);
            } else {
                const std::size_t half = rec.data.size() / 2;
                const std::string pages[2] = {rec.data.substr(0, half), rec.data.substr(half)};
                put32(payload, 0xBB106BB1u);
                put64(payload, rec.data.size());
                put32(payload, 2);
                put32(payload, 32 + 16);
                put32(payload, static_cast<std::uint32_t>(half));
                put32(payload, static_cast<std::uint32_t>(pages[1].size()));
                put32(payload, 0); put32(payload, 0);
                std::uint64_t start = 0;
                for (std::uint32_t k = 0; k < 2; ++k) {
                    put32(payload, 6 + k);
                    put32(payload, static_cast<std::uint32_t>(pages[k].size()));
                    std::string b = seg_header("blob01", 6 + k, 0, 0, 0, true);
                    put64(b, rec.data.size()); put64(b, start); put32(b, k); put32(b, 2);
                    put64(b, pages[k].size());
                    b += pages[k];
                    start += pages[k].size();
                    pad_to(b, (b.size() + 0x7F) / 0x80 * 0x80, 0x70);
                    set32(b, 0x10, static_cast<std::uint32_t>(b.size()));
                    segs[6 + k] = b;
                }
            }
            datidx.emplace_back(add(rec.handle, payload), rec.schema);
        }
        const std::size_t data_at = (0x30 + headers.size() + 15) / 16 * 16;
        std::string seg = seg_header("_data_", 5, 0, 0, static_cast<std::uint32_t>(data_at / 16), false);
        seg += headers;
        pad_to(seg, data_at, 'b');
        seg += data;
        pad_to(seg, (seg.size() + 0x7F) / 0x80 * 0x80, 0x70);
        set32(seg, 0x10, static_cast<std::uint32_t>(seg.size()));
        segs[5] = seg;
    }
    // datidx: one stub entry first.
    {
        std::string body;
        put32(body, static_cast<std::uint32_t>(datidx.size() + 1)); put32(body, 0);
        put32(body, 0); put32(body, 0); put32(body, 0);
        for (const auto& [local, schema] : datidx) { put32(body, 5); put32(body, local); put32(body, schema); }
        std::string seg = seg_header("datidx", 2, 0, 0, 0, false) + body;
        pad_to(seg, (seg.size() + 0x7F) / 0x80 * 0x80, 0x70);
        set32(seg, 0x10, static_cast<std::uint32_t>(seg.size()));
        segs[2] = seg;
    }
    // Layout: header, segments 2.., then segidx last (its offsets are known by then).
    std::string out(0x80, '\0');
    std::memcpy(out.data(), "jard", 4);
    std::map<std::uint32_t, std::pair<std::uint64_t, std::uint32_t>> where;
    for (const auto& [idx, seg] : segs) {
        where[idx] = {out.size(), static_cast<std::uint32_t>(seg.size())};
        out += seg;
    }
    const std::uint32_t nseg = 8;
    where[1] = {out.size(), 0};
    std::string body;
    for (std::uint32_t i = 0; i < nseg; ++i) {
        const auto it = where.find(i);
        put64(body, it == where.end() ? 0 : it->second.first);
        put32(body, it == where.end() ? 0 : it->second.second);
    }
    std::string seg = seg_header("segidx", 1, 0, 0, 0, false) + body;
    pad_to(seg, (seg.size() + 0x7F) / 0x80 * 0x80, 0x70);
    set32(seg, 0x10, static_cast<std::uint32_t>(seg.size()));
    set32(out, 0x18, static_cast<std::uint32_t>(out.size()));
    out += seg;
    set32(out, 0x20, nseg);
    set32(out, 0x24, 3);
    set32(out, 0x28, 2);
    return out;
}

// ---- R2004 container (spec §4) ---------------------------------------------------------------

std::uint32_t checksum(std::uint32_t seed, const std::string& d) {
    std::uint32_t s1 = seed & 0xFFFF, s2 = seed >> 16;
    for (std::size_t i = 0; i < d.size();) {
        const std::size_t n = std::min<std::size_t>(0x15B0, d.size() - i);
        for (std::size_t k = 0; k < n; ++k) { s1 += static_cast<unsigned char>(d[i + k]); s2 += s1; }
        s1 %= 0xFFF1; s2 %= 0xFFF1;
        i += n;
    }
    return s2 << 16 | (s1 & 0xFFFF);
}

// Literal-only LZ77 stream (one literal run, then the 0x11 terminator); needs >= 4 bytes.
std::string lz_literal(const std::string& d) {
    std::string s;
    const std::size_t n = d.size();
    REQUIRE(n >= 4);
    if (n - 3 <= 0x0F) {
        s += char(n - 3);
    } else {
        s += '\0';
        std::size_t r = n - 3 - 0x0F;
        while (r > 0xFF) { s += '\0'; r -= 0xFF; }
        s += char(r);
    }
    return s + d + '\x11';
}

std::string build_dwg(const std::string& version,
                      const std::vector<std::pair<std::string, std::string>>& sections) {
    std::string file(0x100, '\0');
    std::memcpy(file.data(), version.data(), 6);
    std::vector<std::pair<std::int32_t, std::uint32_t>> page_map;  // number, extent
    std::string section_map;
    put32(section_map, static_cast<std::uint32_t>(sections.size()));
    put32(section_map, 2); put32(section_map, 0x7400); put32(section_map, 0);
    put32(section_map, static_cast<std::uint32_t>(sections.size()));
    std::int32_t page = 1;
    std::uint32_t id = static_cast<std::uint32_t>(sections.size());
    for (const auto& [name, data] : sections) {
        const std::uint64_t addr = file.size();
        const std::string comp = lz_literal(data);
        const std::uint32_t extent = static_cast<std::uint32_t>((32 + comp.size() + 0x1F) / 0x20 * 0x20);
        std::string hdr;
        put32(hdr, 0x4163043b); put32(hdr, id); put32(hdr, static_cast<std::uint32_t>(comp.size()));
        put32(hdr, extent); put64(hdr, 0); put32(hdr, 0);
        const std::uint32_t dsum = checksum(0, comp);
        put32(hdr, dsum);
        set32(hdr, 24, checksum(dsum, hdr));
        const std::uint32_t mask = 0x4164536b ^ static_cast<std::uint32_t>(addr);
        for (int j = 0; j < 32; ++j) hdr[j] = char(static_cast<unsigned char>(hdr[j]) ^ (mask >> (8 * (j % 4)) & 0xFF));
        file += hdr + comp;
        pad_to(file, addr + extent, 0);
        page_map.emplace_back(page, extent);
        // section description + its single page
        put64(section_map, data.size());
        put32(section_map, 1); put32(section_map, 0x7400); put32(section_map, 1); put32(section_map, 2);
        put32(section_map, id--); put32(section_map, 0);
        std::string nm = name;
        nm.resize(64, '\0');
        section_map += nm;
        put32(section_map, static_cast<std::uint32_t>(page)); put32(section_map, static_cast<std::uint32_t>(comp.size()));
        put64(section_map, 0);
        ++page;
    }
    auto system_page = [&](std::uint32_t type, const std::string& data) {
        const std::string comp = lz_literal(data);
        std::string hdr;
        put32(hdr, type); put32(hdr, static_cast<std::uint32_t>(data.size()));
        put32(hdr, static_cast<std::uint32_t>(comp.size())); put32(hdr, 2); put32(hdr, 0);
        set32(hdr, 16, checksum(checksum(0, hdr), comp));
        std::string page_bytes = hdr + comp;
        pad_to(page_bytes, (page_bytes.size() + 0x1F) / 0x20 * 0x20, 0);
        return page_bytes;
    };
    const std::int32_t section_map_id = page++;
    const std::string sm_page = system_page(0x4163003b, section_map);
    file += sm_page;
    page_map.emplace_back(section_map_id, static_cast<std::uint32_t>(sm_page.size()));
    const std::int32_t page_map_id = page;
    const std::uint64_t page_map_addr = file.size();
    std::string pm;
    for (const auto& [n, extent] : page_map) { put32(pm, static_cast<std::uint32_t>(n)); put32(pm, extent); }
    put32(pm, static_cast<std::uint32_t>(page_map_id)); put32(pm, 0x100);
    file += system_page(0x41630e3b, pm);

    std::string h(0x6C, '\0');
    std::memcpy(h.data(), "AcFssFcAJMB", 11);
    set32(h, 0x10, 0x6C); set32(h, 0x14, 4);
    set32(h, 0x50, static_cast<std::uint32_t>(page_map_id));
    std::string addr;
    put64(addr, page_map_addr - 0x100);
    std::memcpy(h.data() + 0x54, addr.data(), 8);
    set32(h, 0x5C, static_cast<std::uint32_t>(section_map_id));
    std::uint32_t seed = 1;
    for (auto& c : h) {
        seed = seed * 0x343FD + 0x269EC3;
        c = char(static_cast<unsigned char>(c) ^ (seed >> 16 & 0xFF));
    }
    std::memcpy(file.data() + 0x80, h.data(), h.size());
    return file;
}

const std::string kEnd = std::string("\x0e\x03" "End" "\x0e\x02" "of" "\x0e\x03" "ASM" "\r\x04" "data");

std::string fake_sab(const std::string& tag, std::size_t filler) {
    return "ASM BinaryFile4" + tag + std::string(filler, '\x07') + kEnd;
}

std::string encrypt_sat(const std::string& plain) {
    std::string out = plain;
    for (auto& c : out) {
        const auto u = static_cast<unsigned char>(c);
        if (u > 32) c = char(159 - u);
    }
    return out;
}

const std::string kSat = "400 0 1 0\n@7 ACIS 4.0 @24 Thu Jan  1 00:00:00 2026\n25.4 9.9999999999999995e-07 1e-10\nbody $-1 $1 $-1 $-1 #\nEnd-of-ACIS-data\n";

using Records = std::vector<std::pair<std::uint64_t, std::string>>;

// Table objects every synthetic drawing shares: BLOCK_CONTROL 1, layers 0x10 "0" and
// 0x11 "Parts", *Paper_Space 0x1E, *Model_Space 0x1F and block "Bracket" 0x20.
Records tables(int ver) {
    return {{1, block_control(ver, 0x1F, 0x1E, 0x20)},
            {0x10, named_record(ver, 51, 0x10, 2, "0")},
            {0x11, named_record(ver, 51, 0x11, 2, "Parts")},
            {0x1E, named_record(ver, 49, 0x1E, 1, "*Paper_Space")},
            {0x1F, named_record(ver, 49, 0x1F, 1, "*Model_Space")},
            {0x20, named_record(ver, 49, 0x20, 1, "Bracket")}};
}

// R2004-family drawing: AcDb:AcDbObjects (RL 0x0DCA, then the records), AcDb:Handles with
// offsets into it, AcDb:Classes, optional data store.
std::string container_drawing(int ver, const Records& objs, const std::string& classes,
                              const std::string* acds) {
    std::string objects;
    put32(objects, 0x0DCA);
    std::vector<std::pair<std::uint64_t, std::uint64_t>> map;
    for (const auto& [handle, rec] : objs) {
        map.emplace_back(handle, objects.size());
        objects += rec;
    }
    std::vector<std::pair<std::string, std::string>> sections = {
        {"AcDb:AcDbObjects", objects}, {"AcDb:Handles", object_map(map, 4)}, {"AcDb:Classes", classes}};
    if (acds) sections.emplace_back("AcDb:AcDsPrototype_1b", *acds);
    return build_dwg("AC" + std::to_string(ver), sections);
}

// R13-R2000 flat file (§3.2.6): section locators 0 (header, unused here), 1 (classes) and
// 2 (object map, absolute file offsets), then classes, objects and the map.
std::string flat_drawing(const std::string& version, const Records& objs, const std::string& classes) {
    std::string file(0x19 + 3 * 9, '\0');
    std::memcpy(file.data(), version.data(), 6);
    set32(file, 0x15, 3);
    file += std::string(2, '\0') + std::string(16, '\x95');  // header CRC + sentinel
    const std::size_t classes_at = file.size();
    file += classes;
    std::vector<std::pair<std::uint64_t, std::uint64_t>> map;
    for (const auto& [handle, rec] : objs) {
        map.emplace_back(handle, file.size());
        file += rec;
    }
    const std::size_t map_at = file.size();
    const std::string m = object_map(map, 4);
    file += m;
    auto locator = [&](int i, char number, std::size_t at, std::size_t size) {
        file[0x19 + 9 * i] = number;
        set32(file, 0x19 + 9 * i + 1, static_cast<std::uint32_t>(at));
        set32(file, 0x19 + 9 * i + 5, static_cast<std::uint32_t>(size));
    };
    locator(0, 0, 0, 0);
    locator(1, 1, classes_at, classes.size());
    locator(2, 2, map_at, m.size());
    return file;
}

struct Fixture {
    std::string sab_solid = fake_sab("solid-316", 40);
    std::string sab_surface = fake_sab("surface-31A", 300);
    std::string stale = fake_sab("STALE", 10);
    std::string acds;
    std::string dwg;

    Fixture() {
        acds = build_acds({{0x22, 0, std::string("\x89PNG\r\n\x1a\n", 8) + std::string(32, 'p'), false},
                           {0x316, 1, sab_solid + "trailing-junk", false},
                           {0x31A, 1, sab_surface, true},
                           {0x900, 1, fake_sab("orphan", 4), false}},
                          stale);
        Records objs = tables(kR2018);
        objs.emplace_back(0x316, entity_record(kR2018, spec(38, 0x316)));
        EntitySpec surf = spec(500, 0x31A);
        surf.entmode = 1;
        objs.emplace_back(0x31A, entity_record(kR2018, surf));
        EntitySpec inl = spec(38, 0x320);
        inl.entmode = 0; inl.owner = 0x20; inl.reactors = 2; inl.xdic = true; inl.dbcolor = true;
        inl.has_ds = false; inl.layer = 0x11;
        const std::string enc = encrypt_sat(kSat);
        inl.sat_blocks = {enc.substr(0, enc.size() / 2), enc.substr(enc.size() / 2)};
        objs.emplace_back(0x320, entity_record(kR2018, inl));
        objs.emplace_back(0x330, entity_record(kR2018, spec(37, 0x330)));
        dwg = container_drawing(kR2018, objs, classes_section(kR2018, "PLANESURFACE"), &acds);
    }
};

std::string temp_file(const std::string& name, const std::string& data) {
    const fs::path p = fs::temp_directory_path() / ("chudware_dwg_native_" + name);
    std::ofstream(p, std::ios::binary | std::ios::trunc) << data;
    return p.string();
}

std::string error_of(const std::function<void()>& f) {
    try {
        f();
    } catch (const std::runtime_error& e) {
        return e.what();
    }
    return {};
}

}  // namespace

TEST_CASE("R2004 LZ77: literal run, overlapping back-reference, bounds") {
    const std::string stream("\x01" "abcd" "\x9C\x00\x11", 8);
    CHECK(dwg_decompress_r2004(stream, 64) == "abcdabcdabcd");
    CHECK(error_of([&] { dwg_decompress_r2004(stream, 10); }).find("exceeds") != std::string::npos);
    const std::string before_start("\x01" "abcd" "\x9C\x02\x11", 8);
    CHECK(error_of([&] { dwg_decompress_r2004(before_start, 64); }).find("before the output start") !=
          std::string::npos);
    CHECK(error_of([&] { dwg_decompress_r2004(std::string("\x05" "ab", 3), 64); }).find("overruns") !=
          std::string::npos);
}

TEST_CASE("AcDs: records keyed by handle and schema; blob01 pages joined; stale records ignored") {
    const Fixture fx;
    const auto recs = read_dwg_acds(fx.acds);
    REQUIRE(recs.size() == 4);
    CHECK(recs[0].handle == 0x22);
    CHECK(recs[0].schema == "AcDb_Thumbnail_Schema");
    CHECK_FALSE(recs[0].asm_data);
    CHECK(recs[1].handle == 0x316);
    CHECK(recs[1].schema == "AcDb3DSolid_ASM_Data");
    CHECK(recs[1].asm_data);
    CHECK(recs[1].data == fx.sab_solid + "trailing-junk");
    CHECK(recs[2].handle == 0x31A);
    CHECK(recs[2].data == fx.sab_surface);  // reassembled from two blob01 pages

    std::string bad = fx.acds;
    const std::size_t at = bad.find("blob01");
    REQUIRE(at != std::string::npos);
    bad[at - 2 + 0x30 + 8] ^= 1;  // first page's start offset
    CHECK(error_of([&] { read_dwg_acds(bad); }).find("blob page 0 is out of order") != std::string::npos);
}

TEST_CASE("read_dwg_native AC1032: SAB per handle, owners incl. implicit spaces, layers, inline SAT") {
    const Fixture fx;
    const DwgNativeInfo info = read_dwg_native(temp_file("good.dwg", fx.dwg));
    CHECK(info.version == "AC1032");
    CHECK(info.model_space == 0x1F);
    REQUIRE(info.acis.size() == 4);

    CHECK(info.acis[0].handle == 0x316);
    CHECK(info.acis[0].type == "3DSOLID");
    CHECK(info.acis[0].owner == 0x1F);  // entity mode 2: implicit *Model_Space
    CHECK(info.acis[0].layer == 0x10);
    CHECK(info.acis[0].data == fx.sab_solid);  // signature through End-of-ASM-data, junk dropped

    CHECK(info.acis[1].handle == 0x31A);
    CHECK(info.acis[1].type == "PLANESURFACE");
    CHECK(info.acis[1].owner == 0x1E);  // entity mode 1: implicit *Paper_Space
    CHECK(info.acis[1].data == fx.sab_surface);

    CHECK(info.acis[2].handle == 0x320);
    CHECK(info.acis[2].owner == 0x20);  // explicit owner behind the colour handle
    CHECK(info.acis[2].layer == 0x11);  // after reactors and xdictionary
    CHECK(info.acis[2].data == kSat);

    CHECK(info.acis[3].handle == 0x330);
    CHECK(info.acis[3].type == "REGION");
    CHECK(info.acis[3].data.empty());

    REQUIRE(info.warnings.size() == 2);
    CHECK(info.warnings[0].find("0x900") != std::string::npos);  // record without an object
    CHECK(info.warnings[1].find("REGION 0x330") != std::string::npos);
}

TEST_CASE("read_dwg_native AC1015: flat file, inline SAT joined across blocks, R2000 handle order") {
    // SAT with CR LF line ends, split inside the token "body" and between a CR and its LF.
    std::string crlf;
    for (const char c : kSat) crlf += c == '\n' ? std::string("\r\n") : std::string(1, c);
    const std::string enc = encrypt_sat(crlf);
    const std::size_t in_token = crlf.find("body") + 2;
    const std::size_t in_crlf = crlf.find("\r\nEnd") + 1;
    REQUIRE(in_token < in_crlf);

    Records objs = tables(kR2000);
    EntitySpec solid = spec(38, 0x316);
    solid.sat_blocks = {enc.substr(0, in_token), enc.substr(in_token, in_crlf - in_token),
                        enc.substr(in_crlf)};
    const std::string solid_rec = entity_record(kR2000, solid);
    objs.emplace_back(0x316, solid_rec);
    EntitySpec surf = spec(500, 0x31A);
    surf.entmode = 1;
    surf.sat_blocks = {encrypt_sat(kSat)};
    objs.emplace_back(0x31A, entity_record(kR2000, surf));
    EntitySpec body = spec(39, 0x320);
    body.entmode = 0; body.owner = 0x20; body.reactors = 1; body.links = true; body.layer = 0x11;
    body.sat_blocks = {encrypt_sat(kSat)};
    objs.emplace_back(0x320, entity_record(kR2000, body));
    EntitySpec region = spec(37, 0x330);
    region.acis_empty = true;
    objs.emplace_back(0x330, entity_record(kR2000, region));
    const std::string dwg = flat_drawing("AC1015", objs, classes_section(kR2000, "SURFACE"));

    const DwgNativeInfo info = read_dwg_native(temp_file("r2000.dwg", dwg));
    CHECK(info.version == "AC1015");
    CHECK(info.model_space == 0x1F);
    CHECK(info.warnings.empty());
    REQUIRE(info.acis.size() == 4);
    CHECK(info.acis[0].type == "3DSOLID");
    CHECK(info.acis[0].owner == 0x1F);
    CHECK(info.acis[0].layer == 0x10);
    CHECK(info.acis[0].data == kSat);  // blocks joined exactly, CR LF -> LF
    CHECK(info.acis[1].type == "SURFACE");
    CHECK(info.acis[1].owner == 0x1E);
    CHECK(info.acis[2].type == "BODY");
    CHECK(info.acis[2].owner == 0x20);
    CHECK(info.acis[2].layer == 0x11);  // after reactor, xdictionary, previous and next links
    CHECK(info.acis[2].data == kSat);
    CHECK(info.acis[3].type == "REGION");
    CHECK(info.acis[3].data.empty());  // ACIS-empty entity: no data, nothing to warn about

    // R13-R2000 have no page checksums; the object CRC still names a damaged entity. The flip
    // lands inside the SAT payload, which no structural check would notice.
    std::string bad = dwg;
    const std::size_t at = bad.find(solid_rec);
    REQUIRE(at != std::string::npos);
    bad[at + solid_rec.size() / 2] ^= 0x01;
    CHECK(error_of([&] { read_dwg_native(temp_file("r2000_bad.dwg", bad)); })
              .find("object 0x316: object CRC mismatch") != std::string::npos);
}

TEST_CASE("read_dwg_native AC1018: inline SAB / SAT version 2, ENC colour name and handle") {
    const std::string old_sab = std::string("ACIS BinaryFile") + std::string(24, '\x04') +
                                std::string("\r\x10" "End-of-ACIS-data", 18);
    Records objs = tables(kR2004);
    EntitySpec solid = spec(38, 0x316);
    solid.inline_v2 = old_sab + "following-wireframe-bytes";
    objs.emplace_back(0x316, entity_record(kR2004, solid));
    EntitySpec surf = spec(500, 0x31A);
    surf.entmode = 0; surf.owner = 0x20; surf.xdic = true; surf.dbcolor = true;
    surf.color_name = true; surf.layer = 0x11;
    surf.inline_v2 = old_sab;
    objs.emplace_back(0x31A, entity_record(kR2004, surf));
    EntitySpec body = spec(39, 0x320);
    body.inline_v2 = kSat.substr(0, kSat.size() - 1) + "trailing";  // SAT text, not SAB
    objs.emplace_back(0x320, entity_record(kR2004, body));
    const std::string dwg = container_drawing(kR2004, objs, classes_section(kR2004, "SURFACE"), nullptr);

    const DwgNativeInfo info = read_dwg_native(temp_file("r2004.dwg", dwg));
    CHECK(info.version == "AC1018");
    CHECK(info.model_space == 0x1F);
    CHECK(info.warnings.empty());
    REQUIRE(info.acis.size() == 3);
    CHECK(info.acis[0].data == old_sab);  // through the one-identifier End-of-ACIS-data marker
    CHECK(info.acis[0].owner == 0x1F);
    CHECK(info.acis[1].type == "SURFACE");
    CHECK(info.acis[1].owner == 0x20);  // behind the AcDbColor handle; colour name read inline
    CHECK(info.acis[1].layer == 0x11);
    CHECK(info.acis[1].data == old_sab);
    CHECK(info.acis[2].data == kSat);
}

TEST_CASE("read_dwg_native: pre-R13 drawings cannot hold ACIS and return an empty list") {
    for (const char* version : {"AC1009", "AC2.10"}) {
        std::string old(0x200, '\0');
        std::memcpy(old.data(), version, 6);
        const DwgNativeInfo info = read_dwg_native(temp_file("old.dwg", old));
        CHECK(info.version == version);
        CHECK(info.acis.empty());
        CHECK(info.model_space == 0);
    }
}

TEST_CASE("read_dwg_native: corrupt input throws a named reason") {
    const Fixture fx;
    CHECK(error_of([&] { read_dwg_native(temp_file("text.dwg", "hello, not a drawing")); })
              .find("not a DWG file") != std::string::npos);
    CHECK(error_of([&] { read_dwg_native(temp_file("short.dwg", fx.dwg.substr(0, 0x90))); })
              .find("encrypted R2004 file header") != std::string::npos);
    CHECK(error_of([&] { read_dwg_native(temp_file("trunc.dwg", fx.dwg.substr(0, fx.dwg.size() - 40))); })
              .find("section page map") != std::string::npos);
    std::string flipped = fx.dwg;
    flipped[0x100 + 32 + 40] ^= 0x10;  // inside the objects page payload
    CHECK(error_of([&] { read_dwg_native(temp_file("flip.dwg", flipped)); }).find("checksum mismatch") !=
          std::string::npos);
    std::string future = fx.dwg;
    std::memcpy(future.data(), "AC1040", 6);
    CHECK(error_of([&] { read_dwg_native(temp_file("future.dwg", future)); })
              .find("unsupported DWG version AC1040") != std::string::npos);
    CHECK(error_of([&] { read_dwg_native((fs::temp_directory_path() / "chudware_dwg_native_missing.dwg").string()); })
              .find("cannot open") != std::string::npos);
}
