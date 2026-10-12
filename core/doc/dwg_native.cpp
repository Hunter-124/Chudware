// Native R13..R2018 DWG reader for ACIS-backed entities — see dwg_native.h.
//
// Section and field references (§) are to the Open Design Specification for .dwg files v5.4.1:
// §3.2 R13-R2000 file header, §4 R2004 container, §5 R2007 container, §2.6/2.7 modular
// chars/shorts, §2.12 object type, §2.13 handles, §2.14 CRCs, §10/§5.8 classes, §20
// object/entity records, §23 object map, §24 data storage.
#include "doc/dwg_native.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <cctype>
#include <fstream>
#include <map>
#include <set>
#include <stdexcept>
#include <tuple>

namespace chudware::doc {
namespace {

[[noreturn]] void fail(const std::string& why) {
    throw std::runtime_error("DWG native read: " + why);
}

std::string hex(std::uint64_t v) {
    char buf[24];
    std::snprintf(buf, sizeof buf, "%llX", static_cast<unsigned long long>(v));
    return buf;
}

// ---- Bounded little-endian byte access ------------------------------------------------------

// Every fixed-width read names the structure it belongs to, so a truncated or lying size field
// surfaces as "<what>: <field> at offset N runs past the end".
struct Bytes {
    std::string_view d;
    std::string what;

    void need(std::uint64_t pos, std::uint64_t n, const char* field) const {
        if (pos > d.size() || n > d.size() - pos)
            fail(what + ": " + field + " at offset " + std::to_string(pos) + " (+" +
                 std::to_string(n) + ") runs past the end (" + std::to_string(d.size()) +
                 " bytes)");
    }
    std::uint8_t u8(std::uint64_t p, const char* f) const {
        need(p, 1, f);
        return static_cast<std::uint8_t>(d[p]);
    }
    std::uint16_t u16(std::uint64_t p, const char* f) const {
        need(p, 2, f);
        return static_cast<std::uint16_t>(static_cast<std::uint8_t>(d[p]) |
                                          static_cast<std::uint8_t>(d[p + 1]) << 8);
    }
    std::uint32_t u32(std::uint64_t p, const char* f) const {
        need(p, 4, f);
        std::uint32_t v = 0;
        for (int i = 3; i >= 0; --i) v = v << 8 | static_cast<std::uint8_t>(d[p + i]);
        return v;
    }
    std::uint64_t u64(std::uint64_t p, const char* f) const {
        need(p, 8, f);
        std::uint64_t v = 0;
        for (int i = 7; i >= 0; --i) v = v << 8 | static_cast<std::uint8_t>(d[p + i]);
        return v;
    }
    std::string_view sub(std::uint64_t p, std::uint64_t n, const char* f) const {
        need(p, n, f);
        return d.substr(p, n);
    }
};

// ---- Bit stream (§2) ------------------------------------------------------------------------

// MSB-first bit reader over [begin, end) bits of `d`; never reads outside that window.
class BitReader {
public:
    BitReader(std::string_view d, std::uint64_t begin, std::uint64_t end, std::string what)
        : d_(d), pos_(begin), end_(end), what_(std::move(what)) {
        if (end_ > static_cast<std::uint64_t>(d_.size()) * 8 || begin > end_)
            fail(what_ + ": bit window [" + std::to_string(begin) + ", " + std::to_string(end) +
                 ") outside its " + std::to_string(d_.size()) + "-byte buffer");
    }
    std::uint64_t pos() const { return pos_; }
    std::uint64_t end() const { return end_; }
    std::uint64_t left() const { return end_ - pos_; }

    unsigned b() {
        if (pos_ >= end_) fail(what_ + ": bit stream overrun at bit " + std::to_string(pos_));
        const auto byte = static_cast<std::uint8_t>(d_[pos_ >> 3]);
        const unsigned bit = byte >> (7 - (pos_ & 7)) & 1u;
        ++pos_;
        return bit;
    }
    unsigned bits(int n) {
        unsigned v = 0;
        for (int i = 0; i < n; ++i) v = v << 1 | b();
        return v;
    }
    unsigned bb() { return bits(2); }
    std::uint8_t rc() { return static_cast<std::uint8_t>(bits(8)); }
    std::uint16_t rs() {
        const std::uint16_t lo = rc();
        return static_cast<std::uint16_t>(lo | rc() << 8);
    }
    std::uint32_t rl() {
        const std::uint32_t lo = rs();
        return lo | static_cast<std::uint32_t>(rs()) << 16;
    }
    double rd() {
        std::uint64_t v = 0;
        for (int i = 0; i < 8; ++i) v |= static_cast<std::uint64_t>(rc()) << (8 * i);
        double out;
        std::memcpy(&out, &v, sizeof out);
        return out;
    }
    std::uint16_t bs() {
        switch (bb()) {
            case 0: return rs();
            case 1: return rc();
            case 2: return 0;
            default: return 256;
        }
    }
    std::uint32_t bl() {
        switch (bb()) {
            case 0: return rl();
            case 1: return rc();
            case 2: return 0;
            default: fail(what_ + ": invalid BL code 3 at bit " + std::to_string(pos_ - 2));
        }
    }
    std::uint64_t bll() {
        const unsigned n = bits(3);
        std::uint64_t v = 0;
        for (unsigned i = 0; i < n; ++i) v |= static_cast<std::uint64_t>(rc()) << (8 * i);
        return v;
    }
    double bd() {
        switch (bb()) {
            case 0: return rd();
            case 1: return 1.0;
            case 2: return 0.0;
            default: fail(what_ + ": invalid BD code 3 at bit " + std::to_string(pos_ - 2));
        }
    }
    // §2.12 R2010+ object type.
    unsigned ot() {
        switch (bb()) {
            case 0: return rc();
            case 1: return rc() + 0x1F0u;
            default: return rs();
        }
    }
    struct Ref {
        unsigned code = 0;
        std::uint64_t value = 0;
    };
    // §2.13 |CODE(4)|COUNTER(4)|bytes MSB first|.
    Ref h() {
        const std::uint8_t cc = rc();
        Ref r;
        r.code = cc >> 4;
        const unsigned n = cc & 15u;
        if (n > 8) fail(what_ + ": handle reference with " + std::to_string(n) + " bytes");
        for (unsigned i = 0; i < n; ++i) r.value = r.value << 8 | rc();
        return r;
    }
    // Absolute handle of `r`; offset codes are relative to `base` (the referencing object).
    std::uint64_t abs(const Ref& r, std::uint64_t base) const {
        switch (r.code) {
            case 0x0: case 0x2: case 0x3: case 0x4: case 0x5: return r.value;
            case 0x6: return base + 1;
            case 0x8: return base - 1;
            case 0xA: return base + r.value;
            case 0xC: return base - r.value;
            default: fail(what_ + ": invalid handle reference code " + std::to_string(r.code));
        }
    }
    void skip_bits(std::uint64_t n) {
        if (n > left())
            fail(what_ + ": " + std::to_string(n) + "-bit field overruns the object (" +
                 std::to_string(left()) + " bits left)");
        pos_ += n;
    }
    std::string bytes(std::uint64_t n) {
        if (n > left() / 8)
            fail(what_ + ": " + std::to_string(n) + "-byte field overruns the object");
        std::string out(static_cast<std::size_t>(n), '\0');
        for (auto& c : out) c = static_cast<char>(rc());
        return out;
    }
    // Pre-R2007 TV: BS length, then that many code-page bytes (a trailing NUL is dropped).
    std::string tv() {
        const unsigned n = bs();
        std::string s = bytes(n);
        if (!s.empty() && s.back() == '\0') s.pop_back();
        return s;
    }
    // R2007+ TU: BS length, then UTF-16LE code units; returned as UTF-8.
    std::string tu() {
        const unsigned n = bs();
        if (static_cast<std::uint64_t>(n) * 16 > left())
            fail(what_ + ": " + std::to_string(n) + "-character string overruns the stream");
        std::string out;
        for (unsigned i = 0; i < n; ++i) {
            const unsigned c = rs();
            if (c == 0) continue;
            if (c < 0x80) {
                out += static_cast<char>(c);
            } else if (c < 0x800) {
                out += static_cast<char>(0xC0 | c >> 6);
                out += static_cast<char>(0x80 | (c & 0x3F));
            } else {
                out += static_cast<char>(0xE0 | c >> 12);
                out += static_cast<char>(0x80 | (c >> 6 & 0x3F));
                out += static_cast<char>(0x80 | (c & 0x3F));
            }
        }
        return out;
    }

private:
    std::string_view d_;
    std::uint64_t pos_;
    std::uint64_t end_;
    std::string what_;
};

// §2.7 modular short (object sizes; unsigned). Advances `p`.
std::uint64_t read_ms(const Bytes& in, std::uint64_t& p) {
    std::uint64_t v = 0;
    for (int shift = 0; shift < 60; shift += 15) {
        const std::uint16_t w = in.u16(p, "modular short");
        p += 2;
        v |= static_cast<std::uint64_t>(w & 0x7FFF) << shift;
        if (!(w & 0x8000)) return v;
    }
    fail(in.what + ": modular short longer than 4 words at offset " + std::to_string(p));
}

// §2.6 modular char; `is_signed` honours the 0x40 negation bit of the final byte.
std::int64_t read_mc(const Bytes& in, std::uint64_t& p, std::uint64_t end, bool is_signed) {
    std::uint64_t v = 0;
    for (int shift = 0; shift < 63; shift += 7) {
        if (p >= end) fail(in.what + ": modular char runs past its block at offset " +
                           std::to_string(p));
        const std::uint8_t c = in.u8(p++, "modular char");
        if (c & 0x80) {
            v |= static_cast<std::uint64_t>(c & 0x7F) << shift;
            continue;
        }
        if (is_signed && (c & 0x40)) {
            v |= static_cast<std::uint64_t>(c & 0x3F) << shift;
            return -static_cast<std::int64_t>(v);
        }
        v |= static_cast<std::uint64_t>(c & 0x7F) << shift;
        return static_cast<std::int64_t>(v);
    }
    fail(in.what + ": modular char longer than 9 bytes at offset " + std::to_string(p));
}

// ---- R2004 container (§4) -------------------------------------------------------------------

constexpr std::uint32_t kPageMapType = 0x41630e3b;
constexpr std::uint32_t kSectionMapType = 0x4163003b;
constexpr std::uint32_t kDataPageType = 0x4163043b;
constexpr std::uint32_t kMaxPageSize = 0x1000000;  // real files use 0x7400; 16 MiB is a hard cap

// §4.2 section page checksum.
std::uint32_t page_checksum(std::uint32_t seed, std::string_view data) {
    std::uint32_t sum1 = seed & 0xFFFF;
    std::uint32_t sum2 = seed >> 16;
    std::size_t i = 0;
    while (i < data.size()) {
        const std::size_t chunk = std::min<std::size_t>(0x15B0, data.size() - i);
        for (std::size_t k = 0; k < chunk; ++k) {
            sum1 += static_cast<std::uint8_t>(data[i + k]);
            sum2 += sum1;
        }
        sum1 %= 0xFFF1;
        sum2 %= 0xFFF1;
        i += chunk;
    }
    return sum2 << 16 | (sum1 & 0xFFFF);
}

struct SectionDesc {
    std::string name;
    std::uint64_t size = 0;
    std::uint32_t max_page = 0;
    std::uint32_t compressed = 0;  // 1 = no, 2 = yes
    std::uint32_t id = 0;
    std::uint32_t encrypted = 0;
    struct Page {
        std::int32_t number = 0;
        std::uint32_t data_size = 0;
        std::uint64_t start = 0;
    };
    std::vector<Page> pages;
};

class Container {
public:
    explicit Container(std::string_view file) : file_{file, "file"} {
        // Decompressed sections cannot legitimately exceed what the pages can expand to; this cap
        // (LZ77 expands at most ~255x) keeps a lying size field from allocating unbounded memory.
        cap_ = std::min<std::uint64_t>(static_cast<std::uint64_t>(file.size()) * 256 + (1u << 24),
                                       std::uint64_t{1} << 31);
        read_header();
        read_page_map();
        read_section_map();
    }

    bool has(std::string_view name) const { return find(name) != nullptr; }
    std::string read(std::string_view name) const {
        const SectionDesc* s = find(name);
        if (!s) fail("required section " + std::string(name) + " is missing");
        return read(*s);
    }

    const SectionDesc* find(std::string_view name) const {
        for (const auto& s : sections_)
            if (s.name == name) return &s;
        return nullptr;
    }

    // The logical section: pages in section-map order, omitted zero pages restored (§4.5).
    std::string read(const SectionDesc& s) const {
        const std::string what = "section " + s.name;
        if (s.encrypted == 1) fail(what + " is encrypted (password-protected drawing)");
        if (s.compressed != 1 && s.compressed != 2)
            fail(what + ": unknown compression flag " + std::to_string(s.compressed));
        if (s.size > cap_)
            fail(what + ": declared size " + std::to_string(s.size) + " exceeds what a " +
                 std::to_string(file_.d.size()) + "-byte file can hold");
        if ((s.size || !s.pages.empty()) && (s.max_page == 0 || s.max_page > kMaxPageSize))
            fail(what + ": invalid page size " + std::to_string(s.max_page));
        std::string out(static_cast<std::size_t>(s.size), '\0');
        std::uint64_t running = 0;
        for (std::size_t i = 0; i < s.pages.size(); ++i) {
            const auto& pg = s.pages[i];
            const std::string pwhat = what + " page " + std::to_string(i);
            if (pg.start < running)
                fail(pwhat + ": start offset " + std::to_string(pg.start) +
                     " overlaps the previous page");
            if ((pg.start - running) % s.max_page != 0)
                fail(pwhat + ": start offset " + std::to_string(pg.start) +
                     " is not on a page boundary");
            if (pg.start >= s.size) fail(pwhat + ": starts past the section end");
            const std::uint64_t logical = std::min<std::uint64_t>(s.max_page, s.size - pg.start);
            const std::string data = read_data_page(s, pg, pwhat);
            if (data.size() < logical)
                fail(pwhat + ": holds " + std::to_string(data.size()) + " bytes, " +
                     std::to_string(logical) + " expected");
            std::memcpy(out.data() + pg.start, data.data(), static_cast<std::size_t>(logical));
            running = pg.start + s.max_page;
        }
        return out;
    }

private:
    void read_header() {
        // §4.1: 0x6C bytes at 0x80, XOR-ed with an LCG byte stream seeded with 1.
        std::string h(file_.sub(0x80, 0x6C, "encrypted R2004 file header"));
        std::uint32_t seed = 1;
        for (auto& c : h) {
            seed = seed * 0x343FD + 0x269EC3;
            c = static_cast<char>(static_cast<std::uint8_t>(c) ^ (seed >> 16 & 0xFF));
        }
        if (h.compare(0, 11, "AcFssFcAJMB") != 0)
            fail("R2004 file header does not decrypt to the AcFssFcAJMB signature");
        const Bytes hb{h, "R2004 file header"};
        page_map_id_ = static_cast<std::int32_t>(hb.u32(0x50, "section page map id"));
        page_map_addr_ = hb.u64(0x54, "section page map address") + 0x100;
        section_map_id_ = static_cast<std::int32_t>(hb.u32(0x5C, "section map id"));
    }

    // §4.3 system page: 0x14-byte header, then compressed data.
    std::string read_system_page(std::uint64_t addr, std::uint32_t type, const char* what) const {
        const std::string w = what;
        const std::uint32_t got = file_.u32(addr, what);
        if (got != type) fail(w + " at 0x" + hex(addr) + " has page type 0x" + hex(got));
        const std::uint32_t usize = file_.u32(addr + 4, what);
        const std::uint32_t csize = file_.u32(addr + 8, what);
        const std::uint32_t ctype = file_.u32(addr + 12, what);
        const std::uint32_t sum = file_.u32(addr + 16, what);
        if (ctype != 2) fail(w + ": unsupported compression type " + std::to_string(ctype));
        const std::string_view comp = file_.sub(addr + 20, csize, what);
        std::string hdr(file_.sub(addr, 20, what));
        std::memset(hdr.data() + 16, 0, 4);
        const std::uint32_t want = page_checksum(page_checksum(0, hdr), comp);
        if (want != sum) fail(w + ": page checksum mismatch (file corrupt)");
        if (usize > cap_) fail(w + ": decompressed size " + std::to_string(usize) + " too large");
        std::string out = dwg_decompress_r2004(comp, usize);
        if (out.size() != usize)
            fail(w + ": decompressed to " + std::to_string(out.size()) + " bytes, " +
                 std::to_string(usize) + " declared");
        return out;
    }

    void read_page_map() {
        const std::string pm = read_system_page(page_map_addr_, kPageMapType, "section page map");
        const Bytes b{pm, "section page map"};
        std::uint64_t addr = 0x100;
        for (std::uint64_t p = 0; p + 8 <= pm.size();) {
            const auto number = static_cast<std::int32_t>(b.u32(p, "page number"));
            const std::uint32_t size = b.u32(p + 4, "page size");
            p += 8;
            if (number < 0) {
                p += 16;  // gap: parent, left, right, 0
            } else if (!pages_.emplace(number, std::pair{addr, std::uint64_t{size}}).second) {
                fail("section page map lists page " + std::to_string(number) + " twice");
            }
            addr += size;
        }
    }

    std::pair<std::uint64_t, std::uint64_t> page(std::int32_t number, const std::string& w) const {
        const auto it = pages_.find(number);
        if (it == pages_.end())
            fail(w + ": page " + std::to_string(number) + " is not in the section page map");
        return it->second;
    }

    void read_section_map() {
        const auto [addr, _] = page(section_map_id_, "section map");
        const std::string sm = read_system_page(addr, kSectionMapType, "section map");
        const Bytes b{sm, "section map"};
        const std::uint32_t count = b.u32(0, "section count");
        if (static_cast<std::uint64_t>(count) * 96 > sm.size())
            fail("section map: " + std::to_string(count) + " sections cannot fit in " +
                 std::to_string(sm.size()) + " bytes");
        std::uint64_t p = 20;
        for (std::uint32_t i = 0; i < count; ++i) {
            SectionDesc s;
            s.size = b.u64(p, "section size");
            const std::uint32_t npages = b.u32(p + 8, "section page count");
            s.max_page = b.u32(p + 12, "section max page size");
            s.compressed = b.u32(p + 20, "section compressed flag");
            s.id = b.u32(p + 24, "section id");
            s.encrypted = b.u32(p + 28, "section encrypted flag");
            const std::string_view name = b.sub(p + 32, 64, "section name");
            s.name = std::string(name.substr(0, name.find('\0')));
            p += 96;
            if (static_cast<std::uint64_t>(npages) * 16 > sm.size() - std::min<std::uint64_t>(p, sm.size()))
                fail("section map: section " + s.name + " lists " + std::to_string(npages) +
                     " pages past the map end");
            for (std::uint32_t k = 0; k < npages; ++k, p += 16)
                s.pages.push_back({static_cast<std::int32_t>(b.u32(p, "page number")),
                                   b.u32(p + 4, "page data size"), b.u64(p + 8, "page start")});
            sections_.push_back(std::move(s));
        }
    }

    // §4.6 masked data page header, then the page payload.
    std::string read_data_page(const SectionDesc& s, const SectionDesc::Page& pg,
                               const std::string& w) const {
        const auto [addr, extent] = page(pg.number, w);
        std::string hdr(file_.sub(addr, 32, "data page header"));
        const auto mask = static_cast<std::uint32_t>(0x4164536b ^ static_cast<std::uint32_t>(addr));
        for (int j = 0; j < 8; ++j)
            for (int k = 0; k < 4; ++k)
                hdr[4 * j + k] = static_cast<char>(static_cast<std::uint8_t>(hdr[4 * j + k]) ^
                                                   (mask >> (8 * k) & 0xFF));
        const Bytes h{hdr, w + " header"};
        if (h.u32(0, "page type") != kDataPageType)
            fail(w + ": page at 0x" + hex(addr) + " is not a data page");
        if (h.u32(4, "section number") != s.id)
            fail(w + ": page belongs to section " + std::to_string(h.u32(4, "section number")));
        const std::uint32_t dsize = h.u32(8, "data size");
        if (h.u64(16, "start offset") != pg.start)
            fail(w + ": page header start offset disagrees with the section map");
        if (static_cast<std::uint64_t>(dsize) + 32 > extent)
            fail(w + ": data size " + std::to_string(dsize) + " exceeds the page extent " +
                 std::to_string(extent));
        const std::string_view data = file_.sub(addr + 32, dsize, "data page payload");
        const std::uint32_t data_sum = page_checksum(0, data);
        if (data_sum != h.u32(28, "data checksum"))
            fail(w + ": data checksum mismatch (file corrupt)");
        const std::uint32_t hdr_sum = h.u32(24, "header checksum");
        std::memset(hdr.data() + 24, 0, 4);
        if (page_checksum(data_sum, hdr) != hdr_sum)
            fail(w + ": header checksum mismatch (file corrupt)");
        if (s.compressed == 1) return std::string(data);
        return dwg_decompress_r2004(data, s.max_page);
    }

    Bytes file_;
    std::uint64_t cap_ = 0;
    std::int32_t page_map_id_ = 0;
    std::uint64_t page_map_addr_ = 0;
    std::int32_t section_map_id_ = 0;
    std::map<std::int32_t, std::pair<std::uint64_t, std::uint64_t>> pages_;  // address, extent
    std::vector<SectionDesc> sections_;
};

// ---- R2007 container (§5) -------------------------------------------------------------------

constexpr std::uint64_t kR21RsCodeword = 255;     // §5.13 RS codeword size (n)
constexpr std::uint64_t kR21RsSystemData = 239;   // §5.13 system pages: RS(255, 239)
constexpr std::uint64_t kR21RsPageData = 251;     // §5.13 data pages: RS(255, 251)
constexpr std::uint64_t kR21PagesStart = 0x480;   // §5.2 page map offsets are relative to this
constexpr std::uint64_t kR21FileHeaderSize = 0x110;
constexpr std::uint64_t kR21MaxPageSize = 0x1000000;  // real files use 0xF800; 16 MiB hard cap

// §5.13.2 RS de-interleave. `src` holds `blocks` interleaved codewords of 255 bytes (byte j of
// block i sits at j * blocks + i); the result is the k data bytes of every block in block order.
// RS error *correction* is not performed: the data bytes are taken as stored, and a corrupted
// page surfaces through the CRC/checksum checks or the decompressor instead.
std::string r21_rs_data(std::string_view src, std::uint64_t blocks, std::uint64_t k,
                        const std::string& what) {
    if (blocks > src.size() / kR21RsCodeword)
        fail(what + ": " + std::to_string(blocks) + " RS blocks do not fit in " +
             std::to_string(src.size()) + " bytes");
    std::string out(static_cast<std::size_t>(blocks * k), '\0');
    for (std::uint64_t i = 0; i < blocks; ++i)
        for (std::uint64_t j = 0; j < k; ++j)
            out[static_cast<std::size_t>(i * k + j)] = src[static_cast<std::size_t>(j * blocks + i)];
    return out;
}

// The byte visiting order shared by the §5.12 CRCs and the §5.4.1 page checksum: every whole
// 8-byte group is taken as bytes 6,7,4,5,2,3,0,1; a 1-3 byte tail in order; a 4-7 byte tail as
// 2,3,0,1 followed by the rest in order.
template <class F>
void r21_visit(std::string_view d, std::size_t begin, std::size_t n, F&& f) {
    static constexpr int kGroup[8] = {6, 7, 4, 5, 2, 3, 0, 1};
    std::size_t p = begin;
    for (; n >= 8; n -= 8, p += 8)
        for (const int k : kGroup) f(static_cast<std::uint8_t>(d[p + k]));
    if (n >= 4) {
        for (const int k : {2, 3, 0, 1}) f(static_cast<std::uint8_t>(d[p + k]));
        for (std::size_t k = 4; k < n; ++k) f(static_cast<std::uint8_t>(d[p + k]));
    } else {
        for (std::size_t k = 0; k < n; ++k) f(static_cast<std::uint8_t>(d[p + k]));
    }
}

// §5.12 lookup tables, generated from the polynomials whose tables the spec lists: the normal
// (MSB-first) table has entry 1 = 0x42F0E1EBA9EA3693 (ECMA-182); the mirrored (LSB-first) table
// has entry 0x80 = 0x95AC9329AC4BC9B5.
struct R21CrcTables {
    std::array<std::uint64_t, 256> normal{};
    std::array<std::uint64_t, 256> mirrored{};
    constexpr R21CrcTables() {
        for (std::uint64_t i = 0; i < 256; ++i) {
            std::uint64_t n = i << 56, m = i;
            for (int b = 0; b < 8; ++b) {
                n = (n & (std::uint64_t{1} << 63)) ? n << 1 ^ 0x42F0E1EBA9EA3693ull : n << 1;
                m = (m & 1) ? m >> 1 ^ 0x95AC9329AC4BC9B5ull : m >> 1;
            }
            normal[i] = n;
            mirrored[i] = m;
        }
    }
};
constexpr R21CrcTables kR21Crc;

// §5.12 seed initialisers.
std::uint64_t r21_seed1(std::uint64_t seed, std::uint64_t len) {
    seed = (seed + len) * 0x343FDull + 0x269EC3ull;
    seed |= seed * (0x343FDull << 32) + (0x269EC3ull << 32);
    return ~seed;
}
std::uint64_t r21_seed2(std::uint64_t seed, std::uint64_t len) {
    seed = (seed + len) * 0x343FDull + 0x269EC3ull;
    seed = seed * ((std::uint64_t{1} << 32) + 0x343FDull) + (len + 0x269EC3ull);
    return ~seed;
}

// §5.12.1 normal CRC (inverted at the end).
std::uint64_t r21_crc_normal(std::uint64_t crc, std::string_view d) {
    r21_visit(d, 0, d.size(), [&](std::uint8_t c) {
        crc = kR21Crc.normal[(c ^ crc >> 56) & 0xFF] ^ crc << 8;
    });
    return ~crc;
}

// §5.12.2 mirrored CRC (not inverted).
std::uint64_t r21_crc_mirrored(std::uint64_t crc, std::string_view d) {
    r21_visit(d, 0, d.size(), [&](std::uint8_t c) {
        crc = kR21Crc.mirrored[(crc ^ c) & 0xFF] ^ crc >> 8;
    });
    return crc;
}

// §5.4.1 32-bit data page checksum.
std::uint32_t r21_page_checksum(std::uint64_t seed, std::string_view d) {
    seed = (seed + d.size()) * 0x343FDull + 0x269EC3ull;
    std::uint32_t sum1 = static_cast<std::uint32_t>(seed & 0xFFFF);
    std::uint32_t sum2 = static_cast<std::uint32_t>(seed >> 16 & 0xFFFF);
    for (std::size_t p = 0; p < d.size();) {
        const std::size_t chunk = std::min<std::size_t>(0x15B0, d.size() - p);
        r21_visit(d, p, chunk, [&](std::uint8_t c) {
            sum1 += c;
            sum2 += sum1;
        });
        sum1 %= 0xFFF1;
        sum2 %= 0xFFF1;
        p += chunk;
    }
    return sum2 << 16 | (sum1 & 0xFFFF);
}

// §5.10 literal run copy: whole 32-byte groups as 16[16], 16[0]; a 1-31 byte tail as the spec's
// per-length block sequence. Block widths: 1 = one byte, 2/3 = bytes reversed, 4/8 = bytes in
// order, 16 = 8[+8] then 8[+0]. Each row lists (width, source offset) pairs, 0-terminated.
void r21_copy_literal(std::string& out, std::string_view src, std::size_t p, std::size_t n) {
    static constexpr std::uint8_t kTail[32][10] = {
        {},
        {1, 0},
        {1, 1, 1, 0},
        {1, 2, 1, 1, 1, 0},
        {4, 0},
        {1, 4, 4, 0},
        {1, 5, 4, 1, 1, 0},
        {2, 5, 4, 1, 1, 0},
        {8, 0},
        {1, 8, 8, 0},
        {1, 9, 8, 1, 1, 0},
        {2, 9, 8, 1, 1, 0},
        {4, 8, 8, 0},
        {1, 12, 4, 8, 8, 0},
        {1, 13, 4, 9, 8, 1, 1, 0},
        {2, 13, 4, 9, 8, 1, 1, 0},
        {16, 0},
        {8, 9, 1, 8, 8, 0},
        {1, 17, 16, 1, 1, 0},
        {3, 16, 16, 0},
        {4, 16, 16, 0},
        {1, 20, 4, 16, 16, 0},
        {2, 20, 4, 16, 16, 0},
        {3, 20, 4, 16, 16, 0},
        {8, 16, 16, 0},
        {8, 17, 1, 16, 16, 0},
        {1, 25, 8, 17, 1, 16, 16, 0},
        {2, 25, 8, 17, 1, 16, 16, 0},
        {4, 24, 8, 16, 16, 0},
        {1, 28, 4, 24, 8, 16, 16, 0},
        {2, 28, 4, 24, 8, 16, 16, 0},
        {1, 30, 4, 26, 8, 18, 16, 2, 2, 0},
    };
    auto block = [&](std::size_t base, unsigned width, unsigned off) {
        const char* s = src.data() + base + off;
        switch (width) {
            case 1: out += s[0]; break;
            case 2: out += s[1]; out += s[0]; break;
            case 3: out += s[2]; out += s[1]; out += s[0]; break;
            case 4: case 8: out.append(s, width); break;
            default: out.append(s + 8, 8); out.append(s, 8); break;  // 16
        }
    };
    for (; n >= 32; n -= 32, p += 32) {
        block(p, 16, 16);
        block(p, 16, 0);
    }
    const auto& row = kTail[n];
    for (std::size_t i = 0; i < 10 && row[i] != 0; i += 2) block(p, row[i], row[i + 1]);
}

// §5.10 R21 decompression. Produces at most `max_out` bytes; every source read and every
// back-reference is bounds-checked.
std::string r21_decompress(std::string_view src, std::size_t max_out, const std::string& what) {
    std::string out;
    out.reserve(std::min<std::size_t>(max_out, src.size() * 8 + 64));
    std::size_t p = 0;
    auto byte = [&]() -> std::uint32_t {
        if (p >= src.size())
            fail(what + ": R21 compressed stream truncated at byte " + std::to_string(p));
        return static_cast<std::uint8_t>(src[p++]);
    };
    // §5.10.2 ReadInstructions: back-reference length and offset; `op` becomes the next opcode.
    auto instructions = [&](std::uint32_t& op, std::uint32_t& offset, std::uint32_t& length) {
        switch (op >> 4) {
            case 0:
                length = (op & 0xF) + 0x13;
                offset = byte();
                op = byte();
                length += op >> 3 & 0x10;
                offset += ((op & 0x78) << 5) + 1;
                break;
            case 1:
                length = (op & 0xF) + 3;
                offset = byte();
                op = byte();
                offset += ((op & 0xF8) << 5) + 1;
                break;
            case 2:
                offset = byte();
                offset |= byte() << 8;
                length = op & 7;
                if ((op & 8) == 0) {
                    op = byte();
                    length += op & 0xF8;
                } else {
                    ++offset;
                    length += byte() << 3;
                    op = byte();
                    length += ((op & 0xF8) << 8) + 0x100;
                }
                break;
            default:
                length = op >> 4;
                offset = op & 0xF;
                op = byte();
                offset += ((op & 0xF8) << 1) + 1;
                break;
        }
    };

    std::uint32_t op = byte();
    std::uint64_t lit = 0;
    if ((op & 0xF0) == 0x20) {
        p += 2;
        lit = byte() & 7u;
        if (lit == 0) fail(what + ": R21 stream starts with a zero literal length");
    }
    while (p < src.size()) {
        if (lit == 0) {
            // §5.10.1 ReadLiteralLength.
            lit = op + 8u;
            if (lit == 0x17) {
                std::uint32_t n = byte();
                lit += n;
                if (n == 0xFF) {
                    do {
                        n = byte();
                        n |= byte() << 8;
                        lit += n;
                    } while (n == 0xFFFF);
                }
            }
        }
        if (lit > src.size() - p)
            fail(what + ": R21 literal run of " + std::to_string(lit) + " bytes at byte " +
                 std::to_string(p) + " overruns the input");
        if (lit > max_out - out.size())
            fail(what + ": R21 output exceeds " + std::to_string(max_out) + " bytes");
        r21_copy_literal(out, src, p, static_cast<std::size_t>(lit));
        p += static_cast<std::size_t>(lit);
        lit = 0;
        if (p >= src.size()) break;

        op = byte();
        std::uint32_t offset = 0, length = 0;
        instructions(op, offset, length);
        for (;;) {
            // Back-reference `offset` bytes behind the output end; may overlap itself.
            if (offset == 0 || offset > out.size())
                fail(what + ": R21 back-reference " + std::to_string(offset) +
                     " outside the " + std::to_string(out.size()) + " bytes decoded so far");
            if (length > max_out - out.size())
                fail(what + ": R21 output exceeds " + std::to_string(max_out) + " bytes");
            const std::size_t from = out.size() - offset;
            for (std::size_t i = 0; i < length; ++i) out.push_back(out[from + i]);
            lit = op & 7;
            if (lit != 0 || p >= src.size()) break;
            op = byte();
            if ((op >> 4) == 0) break;  // the opcode is the next literal length
            if ((op >> 4) == 0xF) op &= 0xF;
            instructions(op, offset, length);
        }
    }
    return out;
}

// AC1021 (R2007) container (spec §5): RS-coded header/pages, R21 compression.
class ContainerR2007 {
public:
    explicit ContainerR2007(std::string_view file) : file_{file, "file"} {
        // R21 back-references can expand a few bytes into ~64 KiB, so the cap is generous but
        // finite: no declared size larger than this is ever allocated.
        cap_ = std::min<std::uint64_t>(static_cast<std::uint64_t>(file.size()) * 256 + (1u << 24),
                                       std::uint64_t{1} << 31);
        read_header();
        read_page_map();
        read_section_map();
    }

    bool has(std::string_view name) const { return find(name) != nullptr; }

    // The logical section: pages placed at their offsets, gaps (omitted zero pages, §5.2) zero.
    std::string read(std::string_view name) const {
        const Section* s = find(name);
        if (!s) fail("R2007 section " + std::string(name) + " is not in the section map");
        const std::string what = "R2007 section " + s->name;
        if (s->encrypted == 1) fail(what + " is encrypted (password-protected drawing)");
        if (s->encoding != 1 && s->encoding != 4)
            fail(what + ": unknown page encoding " + std::to_string(s->encoding));
        std::string out(static_cast<std::size_t>(s->size), '\0');
        std::uint64_t running = 0;
        for (std::size_t i = 0; i < s->pages.size(); ++i) {
            const Page& pg = s->pages[i];
            const std::string pwhat = what + " page " + std::to_string(i);
            if (pg.offset < running)
                fail(pwhat + ": offset " + std::to_string(pg.offset) +
                     " overlaps the previous page");
            if (pg.usize > s->size - pg.offset)
                fail(pwhat + ": " + std::to_string(pg.usize) + " bytes at offset " +
                     std::to_string(pg.offset) + " run past the section end (" +
                     std::to_string(s->size) + " bytes)");
            const std::string data = read_data_page(*s, pg, pwhat);
            std::memcpy(out.data() + pg.offset, data.data(), data.size());
            running = pg.offset + pg.usize;
        }
        return out;
    }

private:
    struct Page {
        std::uint64_t offset = 0;  // within the logical section
        std::uint64_t size = 0;
        std::int64_t id = 0;
        std::uint64_t usize = 0;
        std::uint64_t csize = 0;
        std::uint64_t checksum = 0;
        std::uint64_t crc = 0;
    };
    struct Section {
        std::string name;  // UTF-8 (section names are ASCII in practice)
        std::uint64_t size = 0;
        std::uint64_t max_page = 0;
        std::uint64_t encrypted = 0;
        std::uint64_t encoding = 0;  // 1 = RS not interleaved, 4 = interleaved
        std::vector<Page> pages;
    };

    const Section* find(std::string_view name) const {
        for (const auto& s : sections_)
            if (s.name == name) return &s;
        return nullptr;
    }

    void read_header() {
        // §5.2: 0x3D8 bytes at 0x80 hold 3 interleaved RS(255,239) codewords. The trailing 0x28
        // bytes of check data (and the checking-sequence CRC inside the decoded block, which needs
        // the §5.2.1.1.5 encoder state) are not verified; the header CRC64 below covers the data.
        const std::string pe =
            r21_rs_data(file_.sub(0x80, 0x3D8, "R2007 file header"), 3, kR21RsSystemData,
                        "R2007 file header");
        const Bytes pb{pe, "R2007 file header"};
        const std::uint64_t comp_crc = pb.u64(0x10, "compressed data CRC");
        const auto comp_len = static_cast<std::int32_t>(pb.u32(0x18, "compressed length"));
        // §5.2.1.3: the compressed-data CRC covers whichever bytes are stored (compressed or not).
        const bool compressed = comp_len > 0;
        const std::uint64_t len = compressed ? static_cast<std::uint64_t>(comp_len)
                                             : 0 - static_cast<std::uint64_t>(
                                                       static_cast<std::int64_t>(comp_len));
        const std::string_view stored = pb.sub(0x20, len, "stored file header data");
        if (r21_crc_normal(r21_seed2(0, stored.size()), stored) != comp_crc)
            fail("R2007 file header: compressed data CRC mismatch (file corrupt)");
        const std::string h = compressed
                                  ? r21_decompress(stored, kR21FileHeaderSize, "R2007 file header")
                                  : std::string(stored.substr(0, kR21FileHeaderSize));
        if (h.size() != kR21FileHeaderSize)
            fail("R2007 file header: decompressed to " + std::to_string(h.size()) +
                 " bytes, 0x110 expected");
        const Bytes hb{h, "R2007 file header"};
        const std::uint64_t header_crc = hb.u64(0x108, "header CRC64");
        std::string zeroed = h;
        std::memset(zeroed.data() + 0x108, 0, 8);
        if (r21_crc_normal(r21_seed2(0, zeroed.size()), zeroed) != header_crc)
            fail("R2007 file header: CRC64 mismatch (file corrupt)");

        crc_seed_ = hb.u64(0xF0, "CRC seed");
        pm_ = {hb.u64(0x38, "page map offset"), hb.u64(0x50, "page map compressed size"),
               hb.u64(0x58, "page map size"), hb.u64(0x18, "page map correction factor"),
               hb.u64(0x10, "page map compressed CRC"), hb.u64(0x80, "page map CRC")};
        sm_id_ = static_cast<std::int64_t>(hb.u64(0xC0, "section map id"));
        sm_ = {0, hb.u64(0xB0, "section map compressed size"), hb.u64(0xC8, "section map size"),
               hb.u64(0xD8, "section map correction factor"),
               hb.u64(0xD0, "section map compressed CRC"), hb.u64(0xA8, "section map CRC")};
    }

    struct SysPage {
        std::uint64_t offset;  // page map: relative to 0x480; section map: from the page map
        std::uint64_t csize;
        std::uint64_t usize;
        std::uint64_t repeat;
        std::uint64_t ccrc;
        std::uint64_t ucrc;
    };

    // §5.3 system page at file address `addr`: the (compressed) data padded to 8 bytes, repeated
    // `repeat` times, RS(255,239)-encoded interleaved; then optionally R21-compressed. Only the
    // first copy is used (the repeats exist for RS recovery, which is not performed); both of
    // its 64-bit CRCs (§5.3, mirrored, seed from UpdateSeed1) are verified.
    std::string read_system_page(std::uint64_t addr, const SysPage& sp, const char* what) const {
        const std::string w = what;
        if (sp.csize == 0 || sp.csize > file_.d.size())
            fail(w + ": compressed size " + std::to_string(sp.csize) + " is invalid for a " +
                 std::to_string(file_.d.size()) + "-byte file");
        if (sp.usize == 0 || sp.usize > cap_)
            fail(w + ": size " + std::to_string(sp.usize) + " is invalid");
        const std::uint64_t aligned = (sp.csize + 7) & ~std::uint64_t{7};
        if (sp.repeat == 0 || sp.repeat > file_.d.size() / aligned)
            fail(w + ": invalid correction factor " + std::to_string(sp.repeat));
        const std::uint64_t blocks = (aligned * sp.repeat + kR21RsSystemData - 1) / kR21RsSystemData;
        if (blocks > file_.d.size() / kR21RsCodeword)
            fail(w + ": " + std::to_string(blocks) + " RS blocks cannot fit in the file");
        const std::string pe = r21_rs_data(file_.sub(addr, blocks * kR21RsCodeword, what), blocks,
                                           kR21RsSystemData, w);
        const std::string_view comp = std::string_view(pe).substr(0, sp.csize);
        if (r21_crc_mirrored(r21_seed1(crc_seed_, comp.size()), comp) != sp.ccrc)
            fail(w + ": compressed data CRC mismatch (file corrupt)");
        std::string out = sp.csize < sp.usize
                              ? r21_decompress(comp, static_cast<std::size_t>(sp.usize), w)
                              : std::string(comp.substr(0, sp.usize));
        if (out.size() != sp.usize)
            fail(w + ": decoded to " + std::to_string(out.size()) + " bytes, " +
                 std::to_string(sp.usize) + " declared");
        if (r21_crc_mirrored(r21_seed1(crc_seed_, out.size()), out) != sp.ucrc)
            fail(w + ": data CRC mismatch (file corrupt)");
        return out;
    }

    void read_page_map() {
        if (pm_.offset > file_.d.size())
            fail("R2007 page map: offset 0x" + hex(pm_.offset) + " is past the file end");
        const std::string pm =
            read_system_page(kR21PagesStart + pm_.offset, pm_, "R2007 page map");
        const Bytes b{pm, "R2007 page map"};
        if (pm.size() % 16 != 0)
            fail("R2007 page map: size " + std::to_string(pm.size()) +
                 " is not a whole number of entries");
        std::uint64_t addr = kR21PagesStart;
        for (std::uint64_t p = 0; p < pm.size(); p += 16) {
            const std::uint64_t size = b.u64(p, "page size");
            const auto id = static_cast<std::int64_t>(b.u64(p + 8, "page id"));
            if (size > file_.d.size() || addr > file_.d.size() - size)
                fail("R2007 page map: page " + std::to_string(id) + " (" + std::to_string(size) +
                     " bytes at 0x" + hex(addr) + ") runs past the file end");
            // Negative ids are free gaps; only positive ids are referenced.
            if (id > 0 && !pages_.emplace(id, std::pair{addr, size}).second)
                fail("R2007 page map lists page " + std::to_string(id) + " twice");
            addr += size;
        }
    }

    std::pair<std::uint64_t, std::uint64_t> page(std::int64_t id, const std::string& w) const {
        const auto it = pages_.find(id);
        if (it == pages_.end())
            fail(w + ": page " + std::to_string(id) + " is not in the R2007 page map");
        return it->second;
    }

    void read_section_map() {
        const auto [addr, extent] = page(sm_id_, "R2007 section map");
        const std::string sm = read_system_page(addr, sm_, "R2007 section map");
        const Bytes b{sm, "R2007 section map"};
        for (std::uint64_t p = 0; p < sm.size();) {
            Section s;
            s.size = b.u64(p, "section data size");
            s.max_page = b.u64(p + 0x08, "section max size");
            s.encrypted = b.u64(p + 0x10, "section encryption");
            const std::uint64_t name_len = b.u64(p + 0x20, "section name length");
            s.encoding = b.u64(p + 0x30, "section encoding");
            const std::uint64_t npages = b.u64(p + 0x38, "section page count");
            p += 0x40;
            // The name length counts bytes of UTF-16LE, including the terminating zero unit.
            if (name_len % 2 != 0 || name_len > sm.size() - std::min<std::uint64_t>(p, sm.size()))
                fail("R2007 section map: name length " + std::to_string(name_len) +
                     " at offset " + std::to_string(p - 0x20) + " is invalid");
            for (std::uint64_t k = 0; k < name_len; k += 2) {
                const std::uint16_t c = b.u16(p + k, "section name");
                if (c == 0) break;
                if (c >= 0x80) fail("R2007 section map: non-ASCII section name");
                s.name += static_cast<char>(c);
            }
            p += name_len;
            const std::string what = "R2007 section " + s.name;
            if (s.size > cap_)
                fail(what + ": declared size " + std::to_string(s.size) + " exceeds what a " +
                     std::to_string(file_.d.size()) + "-byte file can hold");
            if (npages > (sm.size() - std::min<std::uint64_t>(p, sm.size())) / 56)
                fail(what + ": " + std::to_string(npages) + " pages run past the section map end");
            if (npages != 0 && (s.max_page == 0 || s.max_page > kR21MaxPageSize))
                fail(what + ": invalid max page size " + std::to_string(s.max_page));
            for (std::uint64_t k = 0; k < npages; ++k, p += 56) {
                Page pg;
                pg.offset = b.u64(p, "page offset");
                pg.size = b.u64(p + 8, "page size");
                pg.id = static_cast<std::int64_t>(b.u64(p + 16, "page id"));
                pg.usize = b.u64(p + 24, "page uncompressed size");
                pg.csize = b.u64(p + 32, "page compressed size");
                pg.checksum = b.u64(p + 40, "page checksum");
                pg.crc = b.u64(p + 48, "page CRC");
                if (pg.usize > s.max_page || pg.csize > pg.usize || pg.offset > s.size)
                    fail(what + " page " + std::to_string(k) + ": inconsistent sizes (offset " +
                         std::to_string(pg.offset) + ", " + std::to_string(pg.csize) + " -> " +
                         std::to_string(pg.usize) + " bytes)");
                s.pages.push_back(pg);
            }
            if (s.name.empty()) {
                // The trailing unnamed entry (§5.2 SectionsAmount = sections + 1) holds no data.
                if (!s.pages.empty() || s.size != 0)
                    fail("R2007 section map: unnamed section with data at offset " +
                         std::to_string(p));
                continue;
            }
            if (find(s.name)) fail("R2007 section map lists " + s.name + " twice");
            sections_.push_back(std::move(s));
        }
    }

    // §5.4 data page: the (compressed) data padded to 8 bytes and RS(255,251)-encoded, then
    // R21-compressed when its compressed size is below its size. Encoding 4 interleaves whole
    // codewords (§5.13.2). Encoding 1 (§5.13.1) stores the data blocks first and the parity after
    // them; ODA shortens the last codeword there (a 78-byte page occupies 96 bytes), so only the
    // data itself is required to lie inside the page.
    std::string read_data_page(const Section& s, const Page& pg, const std::string& w) const {
        const auto [addr, extent] = page(pg.id, w);
        std::string pe;
        if (s.encoding == 4) {
            const std::uint64_t blocks =
                (((pg.csize + 7) & ~std::uint64_t{7}) + kR21RsPageData - 1) / kR21RsPageData;
            if (blocks * kR21RsCodeword > extent)
                fail(w + ": " + std::to_string(blocks) + " RS blocks exceed the " +
                     std::to_string(extent) + "-byte page at 0x" + hex(addr));
            pe = r21_rs_data(file_.sub(addr, blocks * kR21RsCodeword, "data page"), blocks,
                             kR21RsPageData, w);
        } else {
            if (pg.csize > extent)
                fail(w + ": " + std::to_string(pg.csize) + " data bytes exceed the " +
                     std::to_string(extent) + "-byte page at 0x" + hex(addr));
            pe = file_.sub(addr, pg.csize, "data page");
        }
        const std::string_view comp = std::string_view(pe).substr(0, pg.csize);
        if (r21_crc_mirrored(r21_seed1(crc_seed_, comp.size()), comp) != pg.crc)
            fail(w + ": page CRC mismatch (file corrupt)");
        std::string out = pg.csize < pg.usize
                              ? r21_decompress(comp, static_cast<std::size_t>(pg.usize), w)
                              : std::string(comp);
        if (out.size() != pg.usize)
            fail(w + ": decoded to " + std::to_string(out.size()) + " bytes, " +
                 std::to_string(pg.usize) + " declared");
        // §5.4.1 does not name the checksum seed; the file CRC seed (0 in ODA files) matches.
        if (r21_page_checksum(crc_seed_, out) != pg.checksum)
            fail(w + ": data checksum mismatch (file corrupt)");
        return out;
    }

    Bytes file_;
    std::uint64_t cap_ = 0;
    std::uint64_t crc_seed_ = 0;
    SysPage pm_{};
    SysPage sm_{};
    std::int64_t sm_id_ = 0;
    std::map<std::int64_t, std::pair<std::uint64_t, std::uint64_t>> pages_;  // address, size
    std::vector<Section> sections_;
};

// ---- AcDb:Classes (§5.8) --------------------------------------------------------------------

constexpr std::array<std::uint8_t, 16> kClassesSentinel = {0x8D, 0xA1, 0xC4, 0xB8, 0xC4, 0xA9,
                                                          0xF8, 0xC5, 0xC0, 0xDC, 0xF4, 0x5F,
                                                          0xE7, 0xCF, 0xB6, 0x8A};

// Locates an R2007+ string stream that ends at `flag_bit` (the "has strings" bit, §20.1).
// Returns the stream's first bit, or `flag_bit` itself when the flag says there are no strings.
std::uint64_t string_stream_start(std::string_view d, std::uint64_t begin, std::uint64_t flag_bit,
                                  const std::string& what) {
    BitReader f(d, flag_bit, flag_bit + 1, what);
    if (!f.b()) return flag_bit;
    std::uint64_t p = flag_bit;
    if (p < begin + 16) fail(what + ": string stream size field before the data start");
    p -= 16;
    BitReader s(d, p, p + 16, what);
    std::uint64_t size = s.rs();
    if (size & 0x8000) {
        if (p < begin + 16) fail(what + ": string stream size field before the data start");
        p -= 16;
        BitReader hi(d, p, p + 16, what);
        size = (size & 0x7FFF) | static_cast<std::uint64_t>(hi.rs()) << 15;
    }
    if (size > p - begin) fail(what + ": string stream size " + std::to_string(size) +
                               " exceeds the data");
    return p - size;
}

// DWG versions by their "ACnnnn" number; the reader branches on these.
constexpr int kR13 = 1012;
constexpr int kR14 = 1014;
constexpr int kR2000 = 1015;
constexpr int kR2004 = 1018;
constexpr int kR2007 = 1021;
constexpr int kR2010 = 1024;
constexpr int kR2013 = 1027;
constexpr int kR2018 = 1032;

// Class number -> DXF class name. Layouts (§10.1, §10.2, §5.8):
//   R13-R2000: RL size, then {BS num, BS flags, TV app, TV c++, TV dxf, B zombie, BS item}
//              until the data area is exhausted;
//   R2004:     RL size, BS max class number, RC, RC, B, then (max - 499) classes with inline TV
//              strings and the R2004 trailer (BL count, BS version, BS maint, BL, BL);
//   R2007+:    RL size, [RL hsize: R2018, and R2010 maintenance > 3], RL bit size, same header,
//              strings in the section's string stream.
// Returns false (with `why`) when the data does not validate against the layout.
bool parse_classes(std::string_view d, int ver, bool has_hsize, std::map<unsigned, std::string>& out,
                   std::string& why) {
    const Bytes b{d, "AcDb:Classes"};
    try {
        if (d.size() < 20 || std::memcmp(d.data(), kClassesSentinel.data(), 16) != 0) {
            why = "missing start sentinel";
            return false;
        }
        std::map<unsigned, std::string> classes;
        auto add = [&](unsigned i, unsigned num, unsigned item, std::string dxf) {
            if (num < 500 || (item != 0x1F2 && item != 0x1F3)) {
                why = "class " + std::to_string(i) + " has number " + std::to_string(num) +
                      " and item class id 0x" + hex(item);
                return false;
            }
            classes[num] = std::move(dxf);
            return true;
        };
        if (ver < kR2007) {
            const std::uint32_t size = b.u32(16, "class data size");
            if (size > d.size() - 20) {
                why = "class data size " + std::to_string(size) + " exceeds the section";
                return false;
            }
            const std::uint64_t end = 20 + static_cast<std::uint64_t>(size);
            BitReader r(d, 20 * 8, end * 8, "AcDb:Classes");
            if (ver <= kR2000) {
                for (unsigned i = 0; r.pos() / 8 + 1 < end; ++i) {
                    const unsigned num = r.bs();
                    r.bs();  // version / proxy flags
                    r.tv();  // application name
                    r.tv();  // C++ class name
                    std::string dxf = r.tv();
                    r.b();   // was a zombie
                    if (!add(i, num, r.bs(), std::move(dxf))) return false;
                }
            } else {
                const unsigned max_num = r.bs();
                r.rc();
                r.rc();
                if (!r.b() || max_num < 499) {
                    why = "class header (max class number " + std::to_string(max_num) + ") invalid";
                    return false;
                }
                for (unsigned i = 0; i + 500 <= max_num; ++i) {
                    const unsigned num = r.bs();
                    r.bs();  // proxy flags
                    r.tv();  // application name
                    r.tv();  // C++ class name
                    std::string dxf = r.tv();
                    r.b();   // was a zombie
                    const unsigned item = r.bs();
                    r.bl();  // instance count
                    r.bs();  // dwg version
                    r.bs();  // maintenance version
                    r.bl();
                    r.bl();
                    if (!add(i, num, item, std::move(dxf))) return false;
                }
            }
            out = std::move(classes);
            return true;
        }
        std::uint64_t q = 20 + (has_hsize ? 4 : 0);
        const std::uint32_t bitsize = b.u32(q, "class data bit size");
        const std::uint64_t begin = (q + 4) * 8;
        const std::uint64_t flag = q * 8 + static_cast<std::uint64_t>(bitsize) - 1;
        if (bitsize < 32 + 1 || flag >= static_cast<std::uint64_t>(d.size()) * 8) {
            why = "class data bit size " + std::to_string(bitsize) + " out of range";
            return false;
        }
        const std::uint64_t str = string_stream_start(d, begin, flag, "AcDb:Classes");
        BitReader r(d, begin, str, "AcDb:Classes");
        BitReader s(d, str, flag, "AcDb:Classes string stream");
        const unsigned max_num = r.bs();
        r.rc();
        r.rc();
        if (!r.b() || max_num < 499) {
            why = "class header (max class number " + std::to_string(max_num) + ") invalid";
            return false;
        }
        for (unsigned i = 0; i + 500 <= max_num; ++i) {
            const unsigned num = r.bs();
            r.bs();  // proxy flags
            s.tu();  // application name
            s.tu();  // C++ class name
            std::string dxf = s.tu();
            r.b();  // was a zombie
            const unsigned item = r.bs();
            r.bl();  // instance count
            r.bl();  // dwg version (BS in some writers; identical bits for values < 256)
            r.bl();  // maintenance version
            r.bl();
            r.bl();
            if (!add(i, num, item, std::move(dxf))) return false;
        }
        out = std::move(classes);
        return true;
    } catch (const std::runtime_error& e) {
        why = e.what();
        if (why.rfind("DWG native read: ", 0) == 0) why.erase(0, 17);
        return false;
    }
}

// ---- Object map (§23) -----------------------------------------------------------------------

// (handle, offset into AcDb:AcDbObjects) in map order.
std::vector<std::pair<std::uint64_t, std::uint64_t>> parse_handles(std::string_view d,
                                                                  std::uint64_t objects_size) {
    const Bytes b{d, "AcDb:Handles"};
    std::vector<std::pair<std::uint64_t, std::uint64_t>> out;
    std::uint64_t pos = 0;
    for (;;) {
        b.need(pos, 2, "block size");
        const std::uint64_t size =
            static_cast<std::uint64_t>(b.u8(pos, "block size")) << 8 | b.u8(pos + 1, "block size");
        if (size == 2) break;  // terminating block (CRC only)
        if (size < 2) fail("AcDb:Handles: block at offset " + std::to_string(pos) +
                           " has size " + std::to_string(size));
        const std::uint64_t end = pos + size;
        b.need(pos, size + 2, "handle block");
        std::uint64_t p = pos + 2;
        // Both running values restart at zero in every block (§23.1).
        std::uint64_t handle = 0;
        std::int64_t loc = 0;
        while (p < end) {
            const std::int64_t dh = read_mc(b, p, end, false);
            const std::int64_t dl = read_mc(b, p, end, true);
            if (dh <= 0)
                fail("AcDb:Handles: non-increasing handle after 0x" + hex(handle));
            handle += static_cast<std::uint64_t>(dh);
            loc += dl;
            if (loc < 0 || static_cast<std::uint64_t>(loc) >= objects_size)
                fail("AcDb:Handles: handle 0x" + hex(handle) + " points to offset " +
                     std::to_string(loc) + " outside AcDb:AcDbObjects");
            out.emplace_back(handle, static_cast<std::uint64_t>(loc));
        }
        pos = end + 2;
    }
    return out;
}

// ---- Objects (§20, R13 .. R2010+ encodings) -------------------------------------------------

constexpr unsigned kTypeRegion = 37;
constexpr unsigned kType3dSolid = 38;
constexpr unsigned kTypeBody = 39;
constexpr unsigned kTypeBlockControl = 48;
constexpr unsigned kTypeBlockHeader = 49;
constexpr unsigned kTypeLayer = 51;

const std::set<std::string, std::less<>> kAcisClasses = {
    "3DSOLID", "BODY", "REGION", "SURFACE", "PLANESURFACE", "EXTRUDEDSURFACE",
    "LOFTEDSURFACE", "REVOLVEDSURFACE", "SWEPTSURFACE", "NURBSURFACE"};

// REGION/3DSOLID/BODY have fixed type numbers; the SURFACE family are classes (number >= 500).
bool is_acis_type(unsigned type, std::string_view name) {
    if (type == kTypeRegion || type == kType3dSolid || type == kTypeBody) return true;
    return type >= 500 && kAcisClasses.count(name) != 0;
}

struct ObjectRecord {
    int ver = 0;                     // drawing version (kR13 ..)
    std::uint64_t handle = 0;        // the object's own handle
    unsigned type = 0;
    std::uint64_t offset = 0;        // byte offset of the MS size (CRC coverage starts here)
    std::uint64_t start = 0;         // first bit of the object data (after MS / MC)
    std::uint64_t data_begin = 0;    // first bit after the object's own handle
    std::uint64_t data_end = 0;      // first bit of the handle stream; R13/R14: record_end
                                     // until the in-object bit size has been read
    std::uint64_t record_end = 0;    // end of the object data (bits); the RS CRC follows
};

// §2.14.1 object CRC: CRC-16 (reflected polynomial 0xA001) seeded with 0xC0C1 over the size
// fields and the object data. Every object whose contents are returned is checked, so a
// corrupted R13-R2000 file (no page checksums) fails by name instead of yielding garbage.
void check_object_crc(std::string_view objects, const ObjectRecord& rec) {
    static const std::array<std::uint16_t, 256> table = [] {
        std::array<std::uint16_t, 256> t{};
        for (unsigned i = 0; i < 256; ++i) {
            unsigned c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
            t[i] = static_cast<std::uint16_t>(c);
        }
        return t;
    }();
    const std::uint64_t end = rec.record_end / 8;
    const Bytes b{objects, "object 0x" + hex(rec.handle)};
    std::uint16_t crc = 0xC0C1;
    for (const char c : b.sub(rec.offset, end - rec.offset, "object data"))
        crc = static_cast<std::uint16_t>((crc >> 8) ^ table[(crc ^ static_cast<std::uint8_t>(c)) & 0xFF]);
    if (crc != b.u16(end, "object CRC"))
        fail(b.what + ": object CRC mismatch (file corrupt)");
}

// §20.1: MS size, [R2010+: MC handle-stream bits], type (R2010+: §2.12 OT, else BS),
// [R2000-R2007: RL data bit size, relative to `start`], own handle.
ObjectRecord object_at(std::string_view objects, std::uint64_t offset, std::uint64_t map_handle,
                       int ver) {
    const std::string what = "object 0x" + hex(map_handle);
    const Bytes b{objects, what};
    std::uint64_t p = offset;
    const std::uint64_t size = read_ms(b, p);
    ObjectRecord rec;
    rec.ver = ver;
    rec.offset = offset;
    std::uint64_t hbits = 0;
    if (ver >= kR2010) hbits = static_cast<std::uint64_t>(read_mc(b, p, objects.size(), false));
    b.need(p, size + 2, "object data and CRC");
    if (hbits > size * 8)
        fail(what + ": handle stream (" + std::to_string(hbits) + " bits) larger than the object (" +
             std::to_string(size) + " bytes)");
    rec.start = p * 8;
    rec.record_end = (p + size) * 8;
    rec.data_end = rec.record_end - hbits;
    BitReader r(objects, rec.start, rec.record_end, what);
    if (ver >= kR2010) {
        rec.type = r.ot();
    } else {
        rec.type = r.bs();
        if (ver >= kR2000) {
            const std::uint32_t bits = r.rl();
            if (bits > size * 8)
                fail(what + ": data bit size " + std::to_string(bits) + " larger than the object (" +
                     std::to_string(size) + " bytes)");
            rec.data_end = rec.start + bits;
        }
    }
    rec.handle = r.h().value;
    rec.data_begin = r.pos();
    if (rec.data_begin > rec.data_end)
        fail(what + ": object header runs into its handle stream");
    return rec;
}

// R13/R14 carry the data bit size inside the object (after EED, or after the graphic for
// entities); reads it from `r` and fixes `rec.data_end`. No-op for later versions.
void read_r13_bit_size(BitReader& r, ObjectRecord& rec, const std::string& what) {
    if (rec.ver > kR14) return;
    const std::uint64_t end = rec.start + r.rl();
    if (end < r.pos() || end > rec.record_end)
        fail(what + ": data bit size points outside the object");
    rec.data_end = end;
}

// Skips extended entity data (§28): BS size, handle, bytes; repeated until size 0.
void skip_eed(BitReader& r) {
    for (unsigned guard = 0;; ++guard) {
        const unsigned size = r.bs();
        if (size == 0) return;
        r.h();
        r.skip_bits(static_cast<std::uint64_t>(size) * 8);
        if (guard > 1u << 20) fail("extended data chain does not terminate");
    }
}

// §20.1 common non-entity data up to the object-specific fields.
struct ObjectCommon {
    ObjectRecord rec;              // with data_end resolved
    std::uint32_t reactors = 0;
    bool xdic_missing = false;     // R2004+ flag; earlier versions always store the xdictionary
    std::uint64_t data_bit = 0;    // first bit of the object-specific data
};

ObjectCommon object_common(std::string_view objects, const ObjectRecord& in, const std::string& what) {
    ObjectCommon c;
    c.rec = in;
    BitReader r(objects, in.data_begin, in.data_end, what);
    skip_eed(r);
    read_r13_bit_size(r, c.rec, what);
    c.reactors = r.bl();
    if (in.ver >= kR2004) c.xdic_missing = r.b();
    if (in.ver >= kR2013) r.b();  // has data-storage data
    c.data_bit = r.pos();
    if (c.data_bit > c.rec.data_end) fail(what + ": common object data overruns the object");
    return c;
}

// Model / paper space block records from BLOCK_CONTROL (object type 48).
std::pair<std::uint64_t, std::uint64_t> block_control_spaces(std::string_view objects,
                                                             const ObjectRecord& in) {
    const std::string what = "BLOCK_CONTROL 0x" + hex(in.handle);
    const ObjectCommon c = object_common(objects, in, what);
    BitReader r(objects, c.data_bit, c.rec.data_end, what);
    const std::uint32_t entries = r.bl();
    BitReader h(objects, c.rec.data_end, c.rec.record_end, what + " handles");
    if ((static_cast<std::uint64_t>(c.reactors) + entries) * 8 > h.left())
        fail(what + ": " + std::to_string(c.reactors) + " reactors and " +
             std::to_string(entries) + " entries cannot fit its handle stream");
    h.h();  // owner (null)
    for (std::uint32_t i = 0; i < c.reactors; ++i) h.h();
    if (!c.xdic_missing) h.h();
    for (std::uint32_t i = 0; i < entries; ++i) h.h();
    const std::uint64_t ms = h.abs(h.h(), in.handle);
    const std::uint64_t ps = h.abs(h.h(), in.handle);
    return {ms, ps};
}

// Entry name of a BLOCK_HEADER (type 49): its first TV field. R2007+ keeps it in the object's
// string stream (whose "has strings" flag is the last bit before the handle stream).
std::string block_record_name(std::string_view objects, const ObjectRecord& rec) {
    const std::string what = "BLOCK_RECORD 0x" + hex(rec.handle);
    if (rec.ver < kR2007) {
        const ObjectCommon c = object_common(objects, rec, what);
        BitReader r(objects, c.data_bit, c.rec.data_end, what);
        return r.tv();
    }
    if (rec.data_end <= rec.data_begin) fail(what + ": no object data");
    const std::uint64_t str = string_stream_start(objects, rec.data_begin, rec.data_end - 1, what);
    if (str == rec.data_end - 1) fail(what + ": has no string stream");
    BitReader s(objects, str, rec.data_end - 1, what + " string stream");
    return s.tu();
}

struct EntityCommon {
    unsigned entmode = 0;
    bool has_ds = false;
    std::uint64_t owner = 0;  // explicit owner (entmode 0) or 0
    std::uint64_t layer = 0;
    std::uint64_t entity_data_bit = 0;  // first bit of the type-specific data
    std::uint64_t data_end = 0;         // first bit of the handle stream
};

// §20.2 common entity data + common entity handles, per version.
EntityCommon entity_common(std::string_view objects, const ObjectRecord& in) {
    const std::string what = "entity 0x" + hex(in.handle);
    const int ver = in.ver;
    ObjectRecord rec = in;
    BitReader r(objects, rec.data_begin, rec.data_end, what);
    skip_eed(r);
    if (r.b()) {  // graphic (proxy preview) present
        const std::uint64_t n = ver >= kR2010 ? r.bll() : r.rl();
        if (n > r.left() / 8) fail(what + ": graphic image of " + std::to_string(n) +
                                   " bytes overruns the entity");
        r.skip_bits(n * 8);
    }
    read_r13_bit_size(r, rec, what);
    EntityCommon e;
    e.data_end = rec.data_end;
    e.entmode = r.bb();
    const std::uint32_t reactors = r.bl();
    if (ver <= kR14) r.b();  // linetype is BYLAYER (else a linetype handle follows the layer)
    const bool xdic_missing = ver >= kR2004 && r.b();
    const bool nolinks = ver >= kR2004 || r.b();  // previous/next entity links, R13-R2000
    if (ver >= kR2013) e.has_ds = r.b();
    bool color_handle = false;
    if (ver >= kR2004) {
        // ENC: flag byte 0x20 transparency BL, 0x40 DBCOLOR handle, else 0x80 RGB BL; R2004
        // stores the colour / book names (0x41 / 0x42) inline, R2007+ in the string stream.
        const unsigned cflags = r.bs() >> 8;
        if (cflags & 0x20) r.bl();
        color_handle = (cflags & 0x40) != 0;
        if (!color_handle && (cflags & 0x80)) r.bl();
        if (ver < kR2007 && (cflags & 0x41) == 0x41) r.tv();
        if (ver < kR2007 && (cflags & 0x42) == 0x42) r.tv();
    } else {
        r.bs();  // CMC colour index
    }
    r.bd();  // linetype scale
    if (ver >= kR2000) {
        r.bb();  // linetype flags
        r.bb();  // plotstyle flags
    }
    if (ver >= kR2007) {
        r.bb();  // material flags
        r.rc();  // shadow flags
    }
    if (ver >= kR2010) {
        r.b();  // has full visual style
        r.b();  // has face visual style
        r.b();  // has edge visual style
    }
    r.bs();  // invisibility
    if (ver >= kR2000) r.rc();  // lineweight
    e.entity_data_bit = r.pos();
    if (e.entity_data_bit > rec.data_end) fail(what + ": common entity data overruns the entity");

    // Handle stream: [colour], owner (entity mode 0), reactors, [xdictionary], then
    // R13/R14 layer; R2000 [previous, next] layer; R2004+ layer.
    BitReader h(objects, rec.data_end, rec.record_end, what + " handles");
    if (static_cast<std::uint64_t>(reactors) * 8 > h.left())
        fail(what + ": " + std::to_string(reactors) + " reactors cannot fit its handle stream");
    if (color_handle) h.h();
    if (e.entmode == 0) e.owner = h.abs(h.h(), rec.handle);
    for (std::uint32_t i = 0; i < reactors; ++i) h.h();
    if (!xdic_missing) h.h();
    if (ver == kR2000 && !nolinks) {
        h.h();
        h.h();
    }
    e.layer = h.abs(h.h(), rec.handle);
    return e;
}

constexpr std::string_view kSabSignatures[] = {"ASM BinaryFile4", "ACIS BinaryFile"};
// SAB end-of-data markers: ASM 21800+ writes the words as three tagged strings (0x0E 0x0E 0x0D);
// older ACIS SAB (e.g. R2004-R2010 inline version 2) writes one 0x0D identifier.
constexpr std::string_view kSabEnds[] = {"\x0e\x03" "End" "\x0e\x02" "of" "\x0e\x03" "ASM" "\r\x04" "data",
                                         "\x0e\x03" "End" "\x0e\x02" "of" "\x0e\x04" "ACIS" "\r\x04" "data",
                                         "\r\x10" "End-of-ACIS-data",
                                         "\r\x0f" "End-of-ASM-data"};

// The SAB stream inside `blob`: from its signature through the end-of-data marker. Empty and
// `why` set when either is missing.
std::string extract_sab(std::string_view blob, std::string& why) {
    std::size_t start = std::string_view::npos;
    for (const auto sig : kSabSignatures) start = std::min(start, blob.find(sig));
    if (start == std::string_view::npos) {
        why = "no 'ASM BinaryFile' / 'ACIS BinaryFile' signature";
        return {};
    }
    std::size_t stop = std::string_view::npos;
    for (const auto end : kSabEnds) {
        const std::size_t at = blob.find(end, start);
        if (at != std::string_view::npos) stop = std::min(stop, at + end.size());
    }
    if (stop == std::string_view::npos) {
        why = "SAB stream has no End-of-ASM-data / End-of-ACIS-data marker";
        return {};
    }
    return std::string(blob.substr(start, stop - start));
}

// AcDbModelerGeometry data stored inside the entity (§20.4.41; every pre-R2013 drawing, and
// R2013+ entities without a data-storage record): version 1 = SAT text split into blocks of
// at most 4096 bytes with every byte c > 32 stored as 159 - c; version 2 = SAB. The blocks
// are joined exactly (a block boundary may fall inside a token); CR LF and lone CR line ends
// become LF.
std::string inline_acis(std::string_view objects, const ObjectRecord& rec,
                        const EntityCommon& e, std::string& why) {
    BitReader r(objects, e.entity_data_bit, e.data_end, "entity 0x" + hex(rec.handle));
    if (r.b()) return {};  // acis_empty
    r.b();
    const unsigned version = r.bs();
    if (version == 1) {
        std::string sat;
        bool after_cr = false;  // a CR LF pair may straddle a block boundary
        for (;;) {
            const std::uint32_t n = r.bl();
            if (n == 0) break;
            const std::string block = r.bytes(n);
            for (const char c : block) {
                const auto u = static_cast<std::uint8_t>(c);
                const char plain = u > 32 ? static_cast<char>(static_cast<std::uint8_t>(159 - u)) : c;
                if (plain == '\n' && after_cr) {
                    after_cr = false;
                    continue;
                }
                after_cr = plain == '\r';
                sat += after_cr ? '\n' : plain;
            }
        }
        if (sat.empty()) why = "inline SAT (version 1) has no data blocks";
        return sat;
    }
    if (version == 2) {
        // §20.4.41: SAB when it starts with the binary signature, else SAT text that ends with
        // its End-of-ACIS-data / End-of-ASM-data line.
        const std::string raw = r.bytes(r.left() / 8);
        if (raw.rfind("ACIS BinaryFile", 0) == 0 || raw.rfind("ASM BinaryFile", 0) == 0)
            return extract_sab(raw, why);
        std::size_t stop = std::string::npos;
        for (const std::string_view end : {std::string_view("End-of-ACIS-data"),
                                           std::string_view("End-of-ASM-data")})
            if (const std::size_t at = raw.find(end); at != std::string::npos)
                stop = std::min(stop, at + end.size());
        if (stop == std::string::npos) {
            why = "inline ACIS (version 2) is neither SAB nor SAT ending in End-of-ACIS-data";
            return {};
        }
        return raw.substr(0, stop) + "\n";
    }
    why = "unknown AcDbModelerGeometry version " + std::to_string(version);
    return {};
}

std::string fixed_type_name(unsigned type) {
    switch (type) {
        case kTypeRegion: return "REGION";
        case kType3dSolid: return "3DSOLID";
        case kTypeBody: return "BODY";
        case kTypeBlockHeader: return "BLOCK_RECORD";
        case kTypeLayer: return "LAYER";
        default: return "object type " + std::to_string(type);
    }
}

std::string read_file(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) fail("cannot open " + path);
    f.seekg(0, std::ios::end);
    const std::streamoff n = f.tellg();
    if (n < 0) fail("cannot size " + path);
    if (static_cast<std::uint64_t>(n) > (std::uint64_t{1} << 31))
        fail(path + " is larger than 2 GiB");
    std::string data(static_cast<std::size_t>(n), '\0');
    f.seekg(0);
    if (!f.read(data.data(), n)) fail("cannot read " + path);
    return data;
}

}  // namespace

// ---- Public API -----------------------------------------------------------------------------

std::string dwg_decompress_r2004(std::string_view src, std::size_t max_out) {
    std::string out;
    out.reserve(std::min<std::size_t>(max_out, src.size() * 8 + 64));
    std::size_t p = 0;
    auto byte = [&]() -> std::uint8_t {
        if (p >= src.size()) fail("LZ77 stream truncated at byte " + std::to_string(p));
        return static_cast<std::uint8_t>(src[p++]);
    };
    // Literal Length: 0x01-0x0F -> +3; 0x00 -> 0x0F + run of 0xFF + final + 3; >= 0x10 -> it is
    // the next opcode and the literal length is 0.
    auto literal_length = [&](std::uint8_t& opcode) -> std::size_t {
        const std::uint8_t c = byte();
        opcode = 0;
        if (c >= 0x01 && c <= 0x0F) return c + 3u;
        if (c == 0) {
            std::size_t total = 0x0F;
            std::uint8_t n;
            while ((n = byte()) == 0) total += 0xFF;
            return total + n + 3;
        }
        opcode = c;
        return 0;
    };
    auto long_offset = [&]() -> std::size_t {
        std::uint8_t c = byte();
        if (c != 0) return c;
        std::size_t total = 0xFF;
        while ((c = byte()) == 0) total += 0xFF;
        return total + c;
    };
    auto two_byte = [&](std::size_t& lit) -> std::size_t {
        const std::uint8_t first = byte();
        const std::uint8_t second = byte();
        lit = first & 3u;
        return static_cast<std::size_t>(first >> 2 | second << 6);
    };
    auto literal = [&](std::size_t n) {
        if (n > src.size() - p) fail("LZ77 literal run of " + std::to_string(n) +
                                     " bytes overruns the input");
        if (n > max_out - out.size()) fail("LZ77 output exceeds " + std::to_string(max_out) +
                                           " bytes");
        out.append(src.substr(p, n));
        p += n;
    };

    std::uint8_t opcode = 0;
    literal(literal_length(opcode));
    while (p < src.size() || opcode != 0) {
        if (opcode == 0) opcode = byte();
        std::size_t count = 0, offset = 0, lit = 0;
        if (opcode == 0x11) break;
        if (opcode >= 0x40) {
            count = (opcode >> 4) - 1u;
            const std::uint8_t op2 = byte();
            offset = static_cast<std::size_t>(op2 << 2 | (opcode & 0x0C) >> 2);
            lit = opcode & 3u;
        } else if (opcode >= 0x21) {
            count = opcode - 0x1Eu;
            offset = two_byte(lit);
        } else if (opcode == 0x20) {
            count = long_offset() + 0x21;
            offset = two_byte(lit);
        } else if (opcode >= 0x12) {
            count = (opcode & 0x0Fu) + 2;
            offset = two_byte(lit) + 0x3FFF;
        } else if (opcode == 0x10) {
            count = long_offset() + 9;
            offset = two_byte(lit) + 0x3FFF;
        } else {
            fail("LZ77 invalid opcode 0x" + hex(opcode) + " at byte " + std::to_string(p - 1));
        }
        opcode = 0;
        if (lit == 0) lit = literal_length(opcode);
        // Back-reference: `offset + 1` bytes behind the output end; may overlap itself.
        if (offset + 1 > out.size())
            fail("LZ77 back-reference " + std::to_string(offset + 1) + " before the output start");
        if (count > max_out - out.size())
            fail("LZ77 output exceeds " + std::to_string(max_out) + " bytes");
        const std::size_t from = out.size() - offset - 1;
        for (std::size_t i = 0; i < count; ++i) out.push_back(out[from + i]);
        literal(lit);
    }
    return out;
}

std::vector<DwgAcdsRecord> read_dwg_acds(std::string_view section) {
    const Bytes b{section, "AcDs data storage"};
    const std::uint32_t segidx_offset = b.u32(0x18, "segment index offset");
    const std::uint32_t num_segidx = b.u32(0x20, "segment index count");
    const std::uint32_t schidx_seg = b.u32(0x24, "schema index segment");
    const std::uint32_t datidx_seg = b.u32(0x28, "data index segment");

    struct Segment {
        std::uint64_t start = 0;
        std::uint64_t size = 0;
        std::uint64_t sys_data = 0;  // start + (system data alignment << 4)
        std::uint64_t obj_data = 0;  // start + (object data alignment << 4)
        std::uint64_t body() const { return start + 0x30; }
        std::uint64_t end() const { return start + size; }
    };
    auto header = [&](std::uint64_t start, std::uint32_t idx, std::string_view name) {
        const std::string w = "AcDs segment " + std::to_string(idx);
        if (b.u16(start, "segment signature") != 0xD5AC)
            fail(w + " at offset " + std::to_string(start) + " has no 0xD5AC signature");
        const std::string_view got = b.sub(start + 2, 6, "segment name");
        if (got != name)
            fail(w + " is '" + std::string(got) + "', expected '" + std::string(name) + "'");
        Segment s;
        s.start = start;
        s.size = b.u32(start + 0x10, "segment size");
        if (s.size < 0x30 || s.size > section.size() - start)
            fail(w + " ('" + std::string(name) + "') size " + std::to_string(s.size) +
                 " exceeds the section");
        s.sys_data = start + (static_cast<std::uint64_t>(b.u32(start + 0x20, "segment data alignment")) << 4);
        s.obj_data = start + (static_cast<std::uint64_t>(b.u32(start + 0x24, "segment object alignment")) << 4);
        if (s.sys_data > s.end() || s.obj_data > s.end())
            fail(w + ": data alignment offset past the segment end");
        return s;
    };

    const Segment segidx = header(segidx_offset, 0, "segidx");
    if (static_cast<std::uint64_t>(num_segidx) * 12 > segidx.size - 0x30)
        fail("AcDs segment index: " + std::to_string(num_segidx) + " entries overrun the segment");
    auto segment = [&](std::uint32_t idx, std::string_view name) {
        if (idx >= num_segidx)
            fail("AcDs segment index " + std::to_string(idx) + " out of range (" +
                 std::to_string(num_segidx) + " entries)");
        const std::uint64_t off = b.u64(segidx.body() + 12ull * idx, "segment offset");
        if (off == 0 || off >= section.size())
            fail("AcDs segment " + std::to_string(idx) + " ('" + std::string(name) +
                 "') has no valid offset");
        return header(off, idx, name);
    };

    // Schema index (§24.2.2.5): schema i -> (schdat segment, local offset); then the schema
    // names, indexed by schema number.
    const Segment schidx = segment(schidx_seg, "schidx");
    std::uint64_t p = schidx.body();
    const std::uint32_t nschema = b.u32(p, "schema count");
    p += 8;
    if (static_cast<std::uint64_t>(nschema) * 12 > schidx.end() - std::min(p, schidx.end()))
        fail("AcDs schema index: " + std::to_string(nschema) + " schemas overrun the segment");
    std::map<std::uint32_t, std::pair<std::uint32_t, std::uint32_t>> schema_at;
    for (std::uint32_t i = 0; i < nschema; ++i, p += 12)
        schema_at[b.u32(p, "schema number")] = {b.u32(p + 4, "schema segment"),
                                                b.u32(p + 8, "schema offset")};
    p += 8;  // 0x0af10c tag
    const std::uint32_t nprops = b.u32(p, "schema property count");
    p += 8;
    if (static_cast<std::uint64_t>(nprops) * 12 > schidx.end() - std::min(p, schidx.end()))
        fail("AcDs schema index: " + std::to_string(nprops) + " properties overrun the segment");
    p += 12ull * nprops;
    const std::uint32_t nnames = b.u32(p, "schema name count");
    p += 4;
    std::vector<std::string> schema_names;
    for (std::uint32_t i = 0; i < nnames; ++i) {
        const std::string_view rest = b.sub(p, schidx.end() - std::min(p, schidx.end()), "schema name");
        const std::size_t z = rest.find('\0');
        if (z == std::string_view::npos) fail("AcDs schema index: unterminated schema name");
        schema_names.emplace_back(rest.substr(0, z));
        p += z + 1;
    }

    // Schema (§24.2.2.6.1): does its payload consist of exactly the binary ASM_Data property?
    std::map<std::uint32_t, bool> asm_cache;
    auto schema_is_asm = [&](std::uint32_t idx) {
        if (auto it = asm_cache.find(idx); it != asm_cache.end()) return it->second;
        const auto at = schema_at.find(idx);
        if (at == schema_at.end())
            fail("AcDs schema " + std::to_string(idx) + " is not in the schema index");
        const Segment sd = segment(at->second.first, "schdat");
        // Property names of this schema data segment live at its system data offset.
        std::uint64_t q = sd.sys_data;
        const std::uint32_t nn = b.u32(q, "property name count");
        q += 4;
        std::vector<std::string> names;
        for (std::uint32_t i = 0; i < nn; ++i) {
            const std::string_view rest = b.sub(q, sd.end() - std::min(q, sd.end()), "property name");
            const std::size_t z = rest.find('\0');
            if (z == std::string_view::npos) fail("AcDs schema data: unterminated property name");
            names.emplace_back(rest.substr(0, z));
            q += z + 1;
        }
        static constexpr std::uint8_t kTypeSize[16] = {0, 0, 2, 1, 2, 4, 8, 1, 2, 4, 8, 4, 8, 0, 0, 0};
        q = sd.body() + at->second.second;
        const std::uint16_t nidx = b.u16(q, "schema index count");
        q += 2 + 8ull * nidx;
        const std::uint16_t np = b.u16(q, "schema property count");
        q += 2;
        int asm_props = 0, other_props = 0;
        for (std::uint16_t i = 0; i < np; ++i) {
            const std::uint32_t flags = b.u32(q, "schema property flags");
            const std::uint32_t name = b.u32(q + 4, "schema property name");
            q += 8;
            std::uint32_t type = 0, size = 0;
            if (!(flags & 2)) {
                type = b.u32(q, "schema property type");
                q += 4;
                if (type > 15) fail("AcDs schema " + std::to_string(idx) + ": property type " +
                                    std::to_string(type));
                if (type == 0xE) {
                    size = b.u32(q, "schema property size");
                    q += 4;
                } else {
                    size = kTypeSize[type];
                }
            }
            if (flags == 1 || flags == 8) q += 4;
            const std::uint16_t nv = b.u16(q, "schema property value count");
            q += 2 + static_cast<std::uint64_t>(nv) * size;
            b.need(q, 0, "schema property values");
            if (name >= names.size())
                fail("AcDs schema " + std::to_string(idx) + ": property name index " +
                     std::to_string(name) + " out of range");
            if (names[name] == "ASM_Data" && type == 15)
                ++asm_props;
            else if (names[name] != "AcDbDs::ID")
                ++other_props;
        }
        return asm_cache[idx] = (asm_props == 1 && other_props == 0);
    };

    // Data index (§24.2.2.2) -> data record headers (§24.2.2.3).
    const Segment datidx = segment(datidx_seg, "datidx");
    const std::uint32_t nentries = b.u32(datidx.body(), "data index entry count");
    if (static_cast<std::uint64_t>(nentries) * 12 > datidx.size - 0x38)
        fail("AcDs data index: " + std::to_string(nentries) + " entries overrun the segment");
    std::vector<DwgAcdsRecord> out;
    std::uint64_t total = 0;
    for (std::uint32_t i = 0; i < nentries; ++i) {
        const std::uint64_t e = datidx.body() + 8 + 12ull * i;
        const std::uint32_t seg = b.u32(e, "data index segment");
        if (seg == 0) continue;  // stub entry
        const std::uint32_t local = b.u32(e + 4, "data index offset");
        const std::uint32_t schema = b.u32(e + 8, "data index schema");
        const std::string w = "AcDs data record " + std::to_string(i);
        const Segment ds = segment(seg, "_data_");
        const std::uint64_t h = ds.body() + local;
        if (h + 20 > ds.obj_data) fail(w + ": header lies outside the record header list");
        const std::uint32_t entry_size = b.u32(h, "record entry size");
        if (entry_size < 20) fail(w + ": entry size " + std::to_string(entry_size));
        DwgAcdsRecord rec;
        rec.handle = b.u64(h + 8, "record handle");
        const std::uint64_t r = ds.obj_data + b.u32(h + 16, "record offset");
        if (schema >= schema_names.size())
            fail(w + ": schema " + std::to_string(schema) + " has no name");
        rec.schema = schema_names[schema];
        rec.asm_data = schema_is_asm(schema);
        if (r + 4 > ds.end()) fail(w + " (handle 0x" + hex(rec.handle) + ") starts past its segment");
        const std::uint32_t size = b.u32(r, "record data size");
        if (size == 0xBB106BB1u) {
            // §24.2.2.3.1 blob reference: the payload is spread over blob01 segments.
            const std::uint64_t q = r + 4;
            const std::uint64_t blob_total = b.u64(q, "blob total size");
            const std::uint32_t npages = b.u32(q + 8, "blob page count");
            if (static_cast<std::uint64_t>(npages) * 8 + 32 > ds.end() - std::min(q, ds.end()))
                fail(w + ": blob page list overruns its segment");
            if (blob_total > section.size())
                fail(w + ": blob of " + std::to_string(blob_total) +
                     " bytes is larger than the data storage section");
            rec.data.reserve(static_cast<std::size_t>(blob_total));
            for (std::uint32_t k = 0; k < npages; ++k) {
                const std::uint32_t pseg = b.u32(q + 32 + 8ull * k, "blob page segment");
                const Segment bs = segment(pseg, "blob01");
                const std::uint64_t pb = bs.body();
                if (b.u64(pb, "blob page total") != blob_total)
                    fail(w + ": blob page " + std::to_string(k) + " disagrees on the total size");
                if (b.u64(pb + 8, "blob page start") != rec.data.size())
                    fail(w + ": blob page " + std::to_string(k) + " is out of order");
                const std::uint64_t n = b.u64(pb + 24, "blob page size");
                if (n > bs.end() - (pb + 32) || n > blob_total - rec.data.size())
                    fail(w + ": blob page " + std::to_string(k) + " size " + std::to_string(n) +
                         " overruns its segment or the blob");
                rec.data.append(b.sub(pb + 32, n, "blob page data"));
            }
            if (rec.data.size() != blob_total)
                fail(w + ": blob pages hold " + std::to_string(rec.data.size()) + " of " +
                     std::to_string(blob_total) + " bytes");
        } else {
            if (static_cast<std::uint64_t>(size) > ds.end() - (r + 4))
                fail(w + " (handle 0x" + hex(rec.handle) + "): " + std::to_string(size) +
                     " bytes overrun its segment");
            rec.data.assign(b.sub(r + 4, size, "record data"));
        }
        total += rec.data.size();
        if (total > section.size() * 2 + 0x10000)
            fail("AcDs data index references the same bytes repeatedly");
        out.push_back(std::move(rec));
    }
    return out;
}

namespace {

// The sections the ACIS reader needs; `acds` is null when the drawing has no data-storage
// section. R13-R2000 have no logical sections: `objects` is then the whole file (the object
// map holds absolute file offsets).
struct DrawingSections {
    int ver = 0;                      // kR13 .. kR2018
    std::string_view objects;
    std::string_view handles;
    std::string_view classes;
    const std::string* acds = nullptr;
    bool classes_hsize_first = true;  // R2007+ AcDb:Classes layout to try first
};

void decode_drawing(const DrawingSections& sec, DwgNativeInfo& info) {
    const std::string_view objects = sec.objects;
    const int ver = sec.ver;
    const auto handles = parse_handles(sec.handles, objects.size());
    std::map<unsigned, std::string> classes;
    std::string why1, why2;
    if (!parse_classes(sec.classes, ver, sec.classes_hsize_first, classes, why1) &&
        (ver < kR2007 ||
         !parse_classes(sec.classes, ver, !sec.classes_hsize_first, classes, why2)))
        fail("AcDb:Classes unreadable (" + why1 + (why2.empty() ? "" : "; " + why2) + ")");

    std::map<std::uint64_t, const DwgAcdsRecord*> asm_records;
    std::vector<DwgAcdsRecord> acds;
    if (sec.acds) {
        acds = read_dwg_acds(*sec.acds);
        for (const auto& r : acds) {
            if (!r.asm_data) continue;
            if (!asm_records.emplace(r.handle, &r).second)
                info.warnings.push_back("data storage holds two ASM_Data records for handle 0x" +
                                        hex(r.handle) + "; the first is used");
        }
    }

    auto type_name = [&](unsigned type) -> std::string {
        if (type >= 500) {
            const auto it = classes.find(type);
            return it == classes.end() ? "unknown class " + std::to_string(type) : it->second;
        }
        return fixed_type_name(type);
    };

    // One pass over the object map: object types, BLOCK_CONTROL, ACIS candidates.
    std::map<std::uint64_t, std::pair<std::uint64_t, unsigned>> by_handle;  // offset, type
    std::vector<ObjectRecord> candidates;
    std::uint64_t paper_space = 0;
    bool have_control = false;
    for (const auto& [handle, offset] : handles) {
        const ObjectRecord rec = object_at(objects, offset, handle, ver);
        if (rec.handle != handle)
            fail("object map entry 0x" + hex(handle) + " points to an object with handle 0x" +
                 hex(rec.handle));
        by_handle[handle] = {offset, rec.type};
        if (rec.type == kTypeBlockControl) {
            if (have_control) fail("drawing has two BLOCK_CONTROL objects");
            check_object_crc(objects, rec);
            std::tie(info.model_space, paper_space) = block_control_spaces(objects, rec);
            have_control = true;
        }
        if (is_acis_type(rec.type, type_name(rec.type)) || asm_records.count(handle))
            candidates.push_back(rec);
    }
    if (!have_control) fail("drawing has no BLOCK_CONTROL object (no model space)");
    const auto ms = by_handle.find(info.model_space);
    if (ms == by_handle.end() || ms->second.second != kTypeBlockHeader)
        fail("BLOCK_CONTROL model space 0x" + hex(info.model_space) + " is not a block record");
    const ObjectRecord ms_rec = object_at(objects, ms->second.first, info.model_space, ver);
    check_object_crc(objects, ms_rec);
    const std::string ms_name = block_record_name(objects, ms_rec);
    std::string ms_upper = ms_name;
    for (auto& ch : ms_upper) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    if (ms_upper != "*MODEL_SPACE")
        info.warnings.push_back("BLOCK_CONTROL model space 0x" + hex(info.model_space) +
                                " is named '" + ms_name + "', not *Model_Space");
    for (const auto& [handle, rec] : asm_records)
        if (!by_handle.count(handle))
            info.warnings.push_back("ASM_Data record for handle 0x" + hex(handle) +
                                    " has no object in the drawing; ignored");

    for (const ObjectRecord& rec : candidates) {
        DwgAcisEntity ent;
        ent.handle = rec.handle;
        ent.type = type_name(rec.type);
        const std::string w = ent.type + " 0x" + hex(rec.handle);
        check_object_crc(objects, rec);
        if (!is_acis_type(rec.type, ent.type)) {
            info.warnings.push_back(w + " carries ASM_Data but is not an ACIS entity type; "
                                    "owner and layer unknown");
        } else {
            try {
                const EntityCommon e = entity_common(objects, rec);
                switch (e.entmode) {
                    case 0: ent.owner = e.owner; break;
                    case 1: ent.owner = paper_space; break;
                    case 2: ent.owner = info.model_space; break;
                    default: fail(w + ": invalid entity mode 3");
                }
                ent.layer = e.layer;
                const auto own = by_handle.find(ent.owner);
                if (own == by_handle.end() || own->second.second != kTypeBlockHeader)
                    info.warnings.push_back(w + ": owner 0x" + hex(ent.owner) +
                                            " is not a block record");
                const auto lay = by_handle.find(ent.layer);
                if (lay == by_handle.end() || lay->second.second != kTypeLayer)
                    info.warnings.push_back(w + ": layer 0x" + hex(ent.layer) +
                                            " is not a LAYER");
                if (!asm_records.count(rec.handle)) {
                    std::string why;
                    if (e.has_ds)
                        why = "flagged as holding data-storage ACIS but no ASM_Data record exists";
                    else
                        ent.data = inline_acis(objects, rec, e, why);
                    if (!why.empty()) info.warnings.push_back(w + ": " + why);
                }
            } catch (const std::runtime_error& err) {
                // One undecodable entity record must not cost the drawing's other bodies.
                ent.owner = ent.layer = 0;
                info.warnings.push_back(w + ": entity record unreadable, owner and layer unknown (" +
                                        err.what() + ")");
            }
        }
        if (const auto it = asm_records.find(rec.handle); it != asm_records.end()) {
            std::string why;
            ent.data = extract_sab(it->second->data, why);
            if (!why.empty())
                info.warnings.push_back(w + ": ASM_Data record unusable (" + why + ")");
        }
        info.acis.push_back(std::move(ent));
    }
}

// R13-R2000 (§3.2.6): numbered section-locator records at 0x15 give the absolute address and
// size of the classes (1) and the object map (2).
std::pair<std::string_view, std::string_view> flat_sections(std::string_view file) {
    const Bytes f{file, "file header"};
    const std::uint32_t n = f.u32(0x15, "section locator count");
    if (n < 3 || n > 16) fail("file header: " + std::to_string(n) + " section locators");
    std::map<unsigned, std::pair<std::uint32_t, std::uint32_t>> loc;
    for (std::uint32_t i = 0; i < n; ++i) {
        const std::uint64_t p = 0x19 + 9ull * i;
        loc[f.u8(p, "section locator")] = {f.u32(p + 1, "section address"),
                                           f.u32(p + 5, "section size")};
    }
    auto get = [&](unsigned number, const char* name) {
        const auto it = loc.find(number);
        if (it == loc.end()) fail(std::string("file header has no locator for the ") + name);
        return f.sub(it->second.first, it->second.second, name);
    };
    return {get(1, "classes section"), get(2, "object map")};
}

// Reads the sections of an R2004-family or R2007 container `c` and decodes the drawing.
template <class C>
void decode_container(const C& c, const std::string& file, int ver, DwgNativeInfo& info) {
    auto section = [&](std::string_view name) -> std::string {
        if (!c.has(name)) fail("required section " + std::string(name) + " is missing");
        return c.read(name);
    };
    const std::string objects = section("AcDb:AcDbObjects");
    const std::string handles = section("AcDb:Handles");
    const std::string classes = section("AcDb:Classes");
    std::string acds;
    const bool has_acds = ver >= kR2013 && c.has("AcDb:AcDsPrototype_1b");
    if (has_acds) acds = c.read("AcDb:AcDsPrototype_1b");
    // AcDb:Classes: R2018 (and R2010+ with maintenance version > 3) carry an extra RL before
    // the bit size; that layout is tried first, the other only when it fails validation.
    const auto maint = static_cast<std::uint8_t>(file.size() > 0x0B ? file[0x0B] : 0);
    decode_drawing({ver, objects, handles, classes, has_acds ? &acds : nullptr,
                    ver >= kR2018 || (ver >= kR2010 && maint > 3)},
                   info);
}

}  // namespace

DwgNativeInfo read_dwg_native(const std::string& path) {
    const std::string file = read_file(path);
    DwgNativeInfo info;
    if (file.size() < 6 || file.compare(0, 2, "AC") != 0 ||
        !std::all_of(file.begin() + 2, file.begin() + 6,
                     [](char c) { return (c >= '0' && c <= '9') || c == '.'; }))
        fail(path + " is not a DWG file (no ACnnnn version signature)");
    info.version = file.substr(0, 6);
    // Pre-R13 drawings ("AC1009" R11/R12 and older, "AC2.10" style) cannot hold ACIS bodies.
    if (info.version.find('.') != std::string::npos || info.version < "AC1012") return info;
    const int ver = std::stoi(info.version.substr(2));
    switch (ver) {
        case kR13:
        case kR14:
        case kR2000: {
            const auto [classes, handles] = flat_sections(file);
            decode_drawing({ver, file, handles, classes, nullptr, false}, info);
            break;
        }
        case kR2007:
            decode_container(ContainerR2007(file), file, ver, info);
            break;
        case kR2004:
        case kR2010:
        case kR2013:
        case kR2018:
            decode_container(Container(file), file, ver, info);
            break;
        default: fail("unsupported DWG version " + info.version);
    }
    return info;
}

}  // namespace chudware::doc
