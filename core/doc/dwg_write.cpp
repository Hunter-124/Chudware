// Native AutoCAD R2000 (AC1015) DWG encoder — see dwg_write.h.
//
// Format reference: Open Design Alliance, "Open Design Specification for .dwg files"
// (R13-R15 file structure, bit codes, object encodings). Layout written here:
//
//   0x00  file header: "AC1015", versions, preview address, codepage, 6 section
//         locators, CRC, sentinel
//   0x61  AuxHeader (section 5)
//         preview (one empty header record)
//         header variables (section 0)   sentinel | RL size | bits | CRC | sentinel
//         classes (section 1)            sentinel | RL size | bits | CRC | sentinel
//         objects, in handle order        MS size | bits | CRC  (each)
//         object map (section 2)          pages of <= 2032 bytes, big-endian size/CRC
//         ObjFreeSpace (section 3)
//         second header                   sentinel | RL size | bits | CRC | junk | sentinel
//         template (section 4)
//
// Every object is one bit stream: type, bit size of the data part, handle, (empty) EED,
// common data, object data, then the handle references starting at the recorded bit
// size. Strings are code page 1252 (header codepage 30). The writer is self-contained:
// it never calls LibreDWG or any other tool.

#include "doc/dwg_write.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <map>
#include <random>
#include <sstream>
#include <stdexcept>
#include <vector>

#include "doc/annot_draw.h"  // annot_text_width: the left-baseline insertion of aligned TEXT
#include "doc/formats.h"     // insunits_mm
#include "doc/sheet_ink.h"   // kInkDash, kInkChain: the DASHED / CENTER patterns

namespace chudware::doc {

namespace {

using Bytes = std::vector<std::uint8_t>;
using P3 = std::array<double, 3>;

// ---------------------------------------------------------------- sentinels

constexpr std::uint8_t kSentinelFileHeaderEnd[16] = {0x95, 0xA0, 0x4E, 0x28, 0x99, 0x82, 0x1A, 0xE5,
                                                     0x5E, 0x41, 0xE0, 0x5F, 0x9D, 0x3A, 0x4D, 0x00};
constexpr std::uint8_t kSentinelPreviewBegin[16] = {0x1F, 0x25, 0x6D, 0x07, 0xD4, 0x36, 0x28, 0x28,
                                                    0x9D, 0x57, 0xCA, 0x3F, 0x9D, 0x44, 0x10, 0x2B};
constexpr std::uint8_t kSentinelPreviewEnd[16] = {0xE0, 0xDA, 0x92, 0xF8, 0x2B, 0xC9, 0xD7, 0xD7,
                                                  0x62, 0xA8, 0x35, 0xC0, 0x62, 0xBB, 0xEF, 0xD4};
constexpr std::uint8_t kSentinelHeaderBegin[16] = {0xCF, 0x7B, 0x1F, 0x23, 0xFD, 0xDE, 0x38, 0xA9,
                                                   0x5F, 0x7C, 0x68, 0xB8, 0x4E, 0x6D, 0x33, 0x5F};
constexpr std::uint8_t kSentinelHeaderEnd[16] = {0x30, 0x84, 0xE0, 0xDC, 0x02, 0x21, 0xC7, 0x56,
                                                 0xA0, 0x83, 0x97, 0x47, 0xB1, 0x92, 0xCC, 0xA0};
constexpr std::uint8_t kSentinelClassesBegin[16] = {0x8D, 0xA1, 0xC4, 0xB8, 0xC4, 0xA9, 0xF8, 0xC5,
                                                    0xC0, 0xDC, 0xF4, 0x5F, 0xE7, 0xCF, 0xB6, 0x8A};
constexpr std::uint8_t kSentinelClassesEnd[16] = {0x72, 0x5E, 0x3B, 0x47, 0x3B, 0x56, 0x07, 0x3A,
                                                  0x3F, 0x23, 0x0B, 0xA0, 0x18, 0x30, 0x49, 0x75};
constexpr std::uint8_t kSentinel2ndHeaderBegin[16] = {0xD4, 0x7B, 0x21, 0xCE, 0x28, 0x93, 0x9F, 0xBF,
                                                      0x53, 0x24, 0x40, 0x09, 0x12, 0x3C, 0xAA, 0x01};
constexpr std::uint8_t kSentinel2ndHeaderEnd[16] = {0x2B, 0x84, 0xDE, 0x31, 0xD7, 0x6C, 0x60, 0x40,
                                                    0xAC, 0xDB, 0xBF, 0xF6, 0xED, 0xC3, 0x55, 0xFE};

// File / AuxHeader / second-header version stamps: AC1015 written by an R2000-class
// application (dwg_version 0x17), maintenance release 6.
constexpr std::uint8_t kMaintRelease = 6;
constexpr std::uint8_t kDwgVersion = 0x17;
constexpr std::uint8_t kMaintVersion = 6;
constexpr std::uint16_t kCodepage1252 = 30;

// Fixed object type numbers (R13-R2000).
enum : std::uint16_t {
    kTEXT = 1, kBLOCK = 4, kENDBLK = 5, kARC = 17, kCIRCLE = 18, kLINE = 19, k3DFACE = 28,
    kSOLID = 31, k3DSOLID = 38, kBODY = 39, kDICTIONARY = 42, kBLOCK_CONTROL = 48,
    kBLOCK_HEADER = 49, kLAYER_CONTROL = 50, kLAYER = 51, kSTYLE_CONTROL = 52, kSTYLE = 53,
    kLTYPE_CONTROL = 56, kLTYPE = 57, kVIEW_CONTROL = 60, kUCS_CONTROL = 62,
    kVPORT_CONTROL = 64, kVPORT = 65, kAPPID_CONTROL = 66, kAPPID = 67,
    kDIMSTYLE_CONTROL = 68, kDIMSTYLE = 69, kVX_CONTROL = 70, kMLINESTYLE = 73,
    kLWPOLYLINE = 77,
    // Classes (section 1), numbered from 500 in declaration order.
    kDICTIONARYWDFLT = 500, kPLACEHOLDER = 501, kLAYOUT = 502,
};

// Handle reference codes.
constexpr int kSoftOwner = 2, kHardOwner = 3, kSoftPtr = 4, kHardPtr = 5;

constexpr int kLineweightByLayer = 29;  // entity RC lineweight index
constexpr int kColorByLayer = 256;

// ---------------------------------------------------------------- CRC-16 (seed 0xC0C1)

std::uint16_t crc16(const std::uint8_t* p, std::size_t n, std::uint16_t crc = 0xC0C1) {
    static const std::array<std::uint16_t, 256> table = [] {
        std::array<std::uint16_t, 256> t{};
        for (unsigned i = 0; i < 256; ++i) {
            unsigned c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? (c >> 1) ^ 0xA001 : c >> 1;
            t[i] = static_cast<std::uint16_t>(c);
        }
        return t;
    }();
    for (std::size_t i = 0; i < n; ++i) crc = static_cast<std::uint16_t>((crc >> 8) ^ table[(crc ^ p[i]) & 0xFF]);
    return crc;
}

// ---------------------------------------------------------------- byte-level output

struct Out {
    Bytes b;
    std::size_t pos() const { return b.size(); }
    void raw(const std::uint8_t* p, std::size_t n) { b.insert(b.end(), p, p + n); }
    void raw(const Bytes& v) { b.insert(b.end(), v.begin(), v.end()); }
    void rc(std::uint8_t v) { b.push_back(v); }
    void rs(std::uint16_t v) { rc(v & 0xFF); rc(v >> 8); }
    void rl(std::uint32_t v) { rs(v & 0xFFFF); rs(static_cast<std::uint16_t>(v >> 16)); }
    void rll(std::uint64_t v) { rl(v & 0xFFFFFFFFu); rl(static_cast<std::uint32_t>(v >> 32)); }
    void rs_be(std::uint16_t v) { rc(v >> 8); rc(v & 0xFF); }
    void zeros(std::size_t n) { b.insert(b.end(), n, 0); }
    // Modular char: 7 bits per byte, low group first, 0x80 = more follows. The signed form
    // keeps bit 0x40 of the last byte for the sign.
    void umc(std::uint64_t v) {
        do {
            std::uint8_t byte = v & 0x7F;
            v >>= 7;
            if (v) byte |= 0x80;
            rc(byte);
        } while (v);
    }
    void mc(std::int64_t sv) {
        const bool neg = sv < 0;
        std::uint64_t v = neg ? static_cast<std::uint64_t>(-sv) : static_cast<std::uint64_t>(sv);
        for (;;) {
            const std::uint8_t group = v & 0x7F;
            v >>= 7;
            if (v == 0 && !(group & 0x40)) {
                rc(static_cast<std::uint8_t>(group | (neg ? 0x40 : 0)));
                return;
            }
            rc(static_cast<std::uint8_t>(group | 0x80));
        }
    }
    // Modular short: 15 bits per little-endian word, 0x8000 = more follows.
    void ms(std::uint32_t v) {
        do {
            std::uint16_t word = v & 0x7FFF;
            v >>= 15;
            if (v) word |= 0x8000;
            rs(word);
        } while (v);
    }
};

// ---------------------------------------------------------------- bit stream

// MSB-first bit writer for the DWG bit codes. Multi-byte raw values (RS/RL/RD) are
// little-endian byte sequences, each byte written MSB-first at the current bit.
class Bits {
public:
    Bytes b;
    std::size_t nbits = 0;

    void bit(bool v) {
        if ((nbits & 7) == 0) b.push_back(0);
        if (v) b[nbits >> 3] |= static_cast<std::uint8_t>(0x80u >> (nbits & 7));
        ++nbits;
    }
    void bits(std::uint64_t v, int n) {
        for (int i = n - 1; i >= 0; --i) bit((v >> i) & 1u);
    }
    void B(bool v) { bit(v); }
    void BB(unsigned v) { bits(v & 3u, 2); }
    void RC(std::uint8_t v) {
        if ((nbits & 7) == 0) {  // aligned fast path
            b.push_back(v);
            nbits += 8;
            return;
        }
        bits(v, 8);
    }
    void RS(std::uint16_t v) { RC(v & 0xFF); RC(static_cast<std::uint8_t>(v >> 8)); }
    void RL(std::uint32_t v) { RS(v & 0xFFFF); RS(static_cast<std::uint16_t>(v >> 16)); }
    void RD(double d) {
        std::uint64_t u;
        std::memcpy(&u, &d, 8);
        for (int i = 0; i < 8; ++i) RC(static_cast<std::uint8_t>(u >> (8 * i)));
    }
    void BS(int sv) {
        const auto v = static_cast<std::uint16_t>(sv);
        if (v == 0) bits(2, 2);
        else if (v == 256) bits(3, 2);
        else if (v < 256) { bits(1, 2); RC(static_cast<std::uint8_t>(v)); }
        else { bits(0, 2); RS(v); }
    }
    void BL(std::uint32_t v) {
        if (v == 0) bits(2, 2);
        else if (v < 256) { bits(1, 2); RC(static_cast<std::uint8_t>(v)); }
        else { bits(0, 2); RL(v); }
    }
    void BD(double v) {
        if (v == 0.0 && !std::signbit(v)) bits(2, 2);
        else if (v == 1.0) bits(1, 2);
        else { bits(0, 2); RD(v); }
    }
    // Default double: 00 when bit-identical to the default, else 11 + the full RD.
    void DD(double v, double def) {
        if (std::memcmp(&v, &def, 8) == 0) bits(0, 2);
        else { bits(3, 2); RD(v); }
    }
    void H(int code, std::uint64_t v) {
        std::uint8_t len = 0;
        for (std::uint64_t t = v; t; t >>= 8) ++len;
        RC(static_cast<std::uint8_t>((code << 4) | len));
        for (int i = len - 1; i >= 0; --i) RC(static_cast<std::uint8_t>(v >> (8 * i)));
    }
    // R13-R2004 text: BS length (no terminator) then the code page 1252 bytes.
    void TV(const std::string& s) {
        BS(static_cast<int>(s.size()));
        for (char c : s) RC(static_cast<std::uint8_t>(c));
    }
    void CMC(int index) { BS(index); }
    void BE(const P3& e) {
        if (e[0] == 0.0 && e[1] == 0.0 && e[2] == 1.0) { B(true); return; }
        B(false);
        BD(e[0]); BD(e[1]); BD(e[2]);
    }
    void BT(double t) {
        if (t == 0.0) { B(true); return; }
        B(false);
        BD(t);
    }
    void RD2(double x, double y) { RD(x); RD(y); }
    void BD3(const P3& p) { BD(p[0]); BD(p[1]); BD(p[2]); }
    void BD3(double x, double y, double z) { BD(x); BD(y); BD(z); }
    void TIMEBLL(std::uint32_t days, std::uint32_t ms) { BL(days); BL(ms); }

    void append(const Bits& o) {
        for (std::size_t i = 0; i < o.nbits; ++i) bit((o.b[i >> 3] >> (7 - (i & 7))) & 1u);
    }
    // Overwrite 32 bits (an RL) starting at bit `at`.
    void patch_rl(std::size_t at, std::uint32_t v) {
        for (int k = 0; k < 32; ++k) {
            const std::size_t p = at + static_cast<std::size_t>((k / 8) * 8 + (7 - k % 8));
            const bool on = (v >> k) & 1u;
            const std::uint8_t mask = static_cast<std::uint8_t>(0x80u >> (p & 7));
            if (on) b[p >> 3] |= mask;
            else b[p >> 3] &= static_cast<std::uint8_t>(~mask);
        }
    }
};

// ---------------------------------------------------------------- objects

// One object: the data stream (type, bit size, handle, EED, common data, fields) and the
// handle-reference stream, joined at the bit size when the object is finished.
class Obj {
public:
    Bits d, h;
    std::uint64_t handle;

    Obj(std::uint16_t type, std::uint64_t handle_) : handle(handle_) {
        d.BS(type);
        bitsize_at_ = d.nbits;
        d.RL(0);           // bit size of the data part, patched in finish()
        d.H(0, handle);
        d.BS(0);           // no extended entity data
    }
    // MS size | object bytes | CRC over both.
    Bytes finish() {
        d.patch_rl(bitsize_at_, static_cast<std::uint32_t>(d.nbits));
        d.append(h);
        Out o;
        o.ms(static_cast<std::uint32_t>(d.b.size()));
        o.raw(d.b);
        o.rs(crc16(o.b.data(), o.b.size()));
        return std::move(o.b);
    }

private:
    std::size_t bitsize_at_ = 0;
};

// Common start of a non-entity object: reactors in the data, then owner / reactors /
// (absent) extension dictionary in the handle stream.
void object_common(Obj& o, std::uint64_t owner, const std::vector<std::uint64_t>& reactors) {
    o.d.BL(static_cast<std::uint32_t>(reactors.size()));
    o.h.H(kSoftPtr, owner);
    for (std::uint64_t r : reactors) o.h.H(kSoftPtr, r);
    o.h.H(kHardOwner, 0);
}

// Table record name and xref flags (data), and the null xref block (handle).
void table_common(Obj& o, const std::string& name) {
    o.d.TV(name);
    o.d.B(true);   // the "64" flag every R13+ table record carries
    o.d.BS(0);     // xref index + 1
    o.d.B(false);  // not xref-dependent
    o.h.H(kHardPtr, 0);
}

struct EntityLinks {
    int entmode = 2;           // 2 model space, 1 paper space
    std::uint64_t layer = 0;
    int ltype_flags = 0;       // 0 ByLayer, 1 ByBlock, 2 Continuous, 3 handle
    std::uint64_t ltype = 0;
    std::uint64_t prev = 0;
    std::uint64_t next = 0;
};

// Common entity data and handles (R2000): no preview, explicit prev/next links.
void entity_common(Obj& o, const EntityLinks& l) {
    o.d.B(false);            // no preview graphics
    o.d.BB(static_cast<unsigned>(l.entmode));
    o.d.BL(0);               // reactors
    o.d.B(false);            // prev/next are written explicitly
    o.d.CMC(kColorByLayer);
    o.d.BD(1.0);             // linetype scale
    o.d.BB(static_cast<unsigned>(l.ltype_flags));
    o.d.BB(0);               // plot style ByLayer
    o.d.BS(0);               // visible
    o.d.RC(kLineweightByLayer);
    o.h.H(kHardOwner, 0);    // no extension dictionary
    o.h.H(kSoftPtr, l.prev);
    o.h.H(kSoftPtr, l.next);
    o.h.H(kHardPtr, l.layer);
    if (l.ltype_flags == 3) o.h.H(kHardPtr, l.ltype);
}

// ---------------------------------------------------------------- helpers

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

// AutoCAD symbol-table names (EXTNAMES=1) exclude < > / \ " : ; ? * | , = ` and control
// characters; those become '_'. Names are at most 255 bytes.
std::string table_name(const std::string& in) {
    std::string out;
    for (unsigned char c : in) {
        if (c < 32 || std::strchr("<>/\\\":;?*|,=`", c)) out.push_back('_');
        else out.push_back(static_cast<char>(c));
        if (out.size() == 255) break;
    }
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out.empty() ? std::string("0") : out;
}

void need_finite(double v, const std::string& what) {
    if (!std::isfinite(v))
        throw std::runtime_error("write_dwg: non-finite coordinate in " + what);
}

double deg2rad(double d) { return d * (3.14159265358979323846 / 180.0); }

struct Box {
    bool ok = false;
    P3 lo{0, 0, 0}, hi{0, 0, 0};
    void add(double x, double y, double z) {
        if (!ok) { lo = hi = {x, y, z}; ok = true; return; }
        lo = {std::min(lo[0], x), std::min(lo[1], y), std::min(lo[2], z)};
        hi = {std::max(hi[0], x), std::max(hi[1], y), std::max(hi[2], z)};
    }
};

// Vertex positions of a SAT body (its `point` records), through the body transform when
// the body carries one: x' = scale * (x * R) + t with ACIS row-vector convention.
void add_sat_points(const std::string& sat, Box& box) {
    std::vector<std::vector<std::string>> records;
    {
        std::vector<std::string> cur;
        std::string tok;
        auto flush_tok = [&] {
            if (!tok.empty()) cur.push_back(tok);
            tok.clear();
        };
        for (char c : sat) {
            if (c == '#') {
                flush_tok();
                records.push_back(std::move(cur));
                cur.clear();
            } else if (c == ' ' || c == '\n' || c == '\r' || c == '\t') {
                flush_tok();
            } else {
                tok.push_back(c);
            }
        }
    }
    auto numbers = [](const std::vector<std::string>& r, std::size_t from) {
        std::vector<double> v;
        for (std::size_t i = from; i < r.size(); ++i) {
            if (r[i].empty() || r[i][0] == '$') continue;
            char* end = nullptr;
            const double x = std::strtod(r[i].c_str(), &end);
            if (end && *end == '\0') v.push_back(x);
        }
        return v;
    };
    auto type_at = [](const std::vector<std::string>& r) -> std::size_t {
        // Records may start with an explicit "-N" index.
        return (!r.empty() && r[0].size() > 1 && r[0][0] == '-' && std::isdigit(static_cast<unsigned char>(r[0][1]))) ? 1 : 0;
    };
    std::array<double, 12> xf{1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0};
    double scale = 1.0;
    for (const auto& r : records) {
        const std::size_t t = type_at(r);
        if (t < r.size() && r[t] == "transform") {
            const std::vector<double> v = numbers(r, t + 1);
            if (v.size() >= 13) {
                std::copy(v.begin(), v.begin() + 12, xf.begin());
                scale = v[12];
            }
            break;
        }
    }
    for (const auto& r : records) {
        const std::size_t t = type_at(r);
        if (t >= r.size() || r[t] != "point") continue;
        const std::vector<double> v = numbers(r, t + 1);
        if (v.size() < 3) continue;
        const double x = v[v.size() - 3], y = v[v.size() - 2], z = v[v.size() - 1];
        box.add(scale * (x * xf[0] + y * xf[3] + z * xf[6]) + xf[9],
                scale * (x * xf[1] + y * xf[4] + z * xf[7]) + xf[10],
                scale * (x * xf[2] + y * xf[5] + z * xf[8]) + xf[11]);
    }
}

// Julian day + milliseconds of the day (UTC) for TIMEBLL / TIMERLL.
std::pair<std::uint32_t, std::uint32_t> julian_now() {
    const auto now = std::chrono::system_clock::now();
    const std::int64_t ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()).count();
    const std::int64_t day_ms = 86400000;
    // Unix epoch = JD 2440587.5, so a civil day starting at UTC midnight is JD n + 0.5.
    const std::int64_t days = ms / day_ms;
    return {static_cast<std::uint32_t>(2440588 + days), static_cast<std::uint32_t>(ms % day_ms)};
}

std::string make_guid(std::mt19937_64& rng) {
    std::uniform_int_distribution<unsigned> hex(0, 15);
    static const char* kDigits = "0123456789ABCDEF";
    std::string g = "{";
    const int groups[5] = {8, 4, 4, 4, 12};
    for (int k = 0; k < 5; ++k) {
        if (k) g.push_back('-');
        for (int i = 0; i < groups[k]; ++i) g.push_back(kDigits[hex(rng)]);
    }
    g.push_back('}');
    return g;
}

// Imperial $INSUNITS codes (inch, foot, mile, microinch, mil, yard, US survey units).
bool imperial_units(int code) {
    switch (code) {
        case 1: case 2: case 3: case 8: case 9: case 10: case 21: case 22: case 23: case 24:
            return true;
        default:
            return false;
    }
}

// ---------------------------------------------------------------- the writer

struct Layer {
    std::string name;
    int color = 7;
    int ltype = 0;   // index into the named linetypes: 0 Continuous, 1 DASHED, 2 CENTER
    std::uint64_t handle = 0;
};

enum class Kind { polyline, arc, line, face, solid, text, acis };
struct Ent {
    Kind kind;
    std::size_t index;
    std::uint64_t handle = 0;
    // Polylines: the vertex run [first, first + count) this LWPOLYLINE carries.
    std::size_t first = 0, count = 0;
    bool closed = false;
};

// LibreDWG's reader — which Chudware's own DWG import runs — rejects an LWPOLYLINE of more
// than 20000 vertices, so longer polylines are written as consecutive LWPOLYLINEs sharing
// their joint vertex: every reader gets the whole outline.
constexpr std::size_t kMaxLwpolylinePoints = 20000;

class Writer {
public:
    explicit Writer(const DxfDocument& doc) : doc_(doc), rng_(std::random_device{}()) {}

    DwgImage build() {
        mm_per_unit_ = insunits_mm(doc_.insunits);
        imperial_ = imperial_units(doc_.insunits);
        assign_layers();
        collect_entities();
        if (ents_.empty()) return {};
        assign_handles();
        compute_extents();
        objects();
        return {assemble(), ents_.size()};
    }

private:
    const DxfDocument& doc_;
    std::mt19937_64 rng_;
    double mm_per_unit_ = 1.0;
    bool imperial_ = false;

    std::vector<Layer> layers_;                  // [0] is layer "0"
    std::map<std::string, std::size_t> by_key_;  // upper-case DWG name -> layers_ index
    std::map<std::string, std::size_t> by_doc_;  // document layer name -> layers_ index
    std::vector<Ent> ents_;
    Box ext_;
    bool three_d_ = false;

    // Handles.
    static constexpr std::uint64_t hBlockCtl = 0x1, hLayerCtl = 0x2, hStyleCtl = 0x3,
                                   hLtypeCtl = 0x5, hViewCtl = 0x6, hUcsCtl = 0x7, hVportCtl = 0x8,
                                   hAppidCtl = 0x9, hDimstyleCtl = 0xA, hVxCtl = 0xB, hNod = 0xC,
                                   hGroupDict = 0xD, hPlotStyleDict = 0xE, hNormal = 0xF,
                                   hLayer0 = 0x10, hStyle = 0x11, hByBlock = 0x12, hByLayer = 0x13,
                                   hContinuous = 0x14, hDashed = 0x15, hCenter = 0x16, hAcad = 0x17,
                                   hDimstyle = 0x18, hVport = 0x19, hMlineDict = 0x1A, hMline = 0x1B,
                                   hPlotSettingsDict = 0x1C, hLayoutDict = 0x1D, hLayoutModel = 0x1E,
                                   hLayout1 = 0x1F, hMsRecord = 0x20, hMsBlock = 0x21, hMsEndblk = 0x22,
                                   hPsRecord = 0x23, hPsBlock = 0x24, hPsEndblk = 0x25,
                                   hFirstFree = 0x26;
    std::uint64_t handseed_ = 0;

    std::map<std::uint64_t, Bytes> objs_;  // handle -> encoded object (map keeps handle order)

    // ------------------------------------------------------------ layers & linetypes

    static int linetype_index(const std::string& name, const std::string& what) {
        const std::string u = upper(name);
        if (u.empty() || u == "CONTINUOUS") return 0;
        if (u == "DASHED") return 1;
        if (u == "CENTER") return 2;
        throw std::runtime_error("write_dwg: unknown linetype '" + name + "' on " + what +
                                 " (DWG export defines CONTINUOUS, DASHED, CENTER)");
    }

    std::uint64_t linetype_handle(int index) const {
        return index == 1 ? hDashed : index == 2 ? hCenter : hContinuous;
    }

    std::size_t layer_slot(const std::string& doc_name) {
        if (auto it = by_doc_.find(doc_name); it != by_doc_.end()) return it->second;
        const std::string dwg = doc_name.empty() ? std::string("0") : table_name(doc_name);
        const std::string key = upper(dwg);
        std::size_t slot;
        if (auto it = by_key_.find(key); it != by_key_.end()) {
            slot = it->second;
        } else {
            slot = layers_.size();
            layers_.push_back({dwg, 7, 0, 0});
            by_key_.emplace(key, slot);
        }
        by_doc_.emplace(doc_name, slot);
        return slot;
    }

    void assign_layers() {
        layers_.push_back({"0", 7, 0, hLayer0});
        by_key_.emplace("0", 0);
        for (const DxfLayerDef& l : doc_.layers) {
            if (l.color < 1 || l.color > 255)
                throw std::runtime_error("write_dwg: layer '" + l.name + "' has colour " +
                                         std::to_string(l.color) + "; a layer needs ACI 1-255");
            const std::size_t slot = layer_slot(l.name);
            layers_[slot].color = l.color;
            layers_[slot].ltype = linetype_index(l.ltype, "layer '" + l.name + "'");
        }
    }

    // ------------------------------------------------------------ entities

    // A polyline whose last point repeats the first (and has more than three points) is a
    // closed LWPOLYLINE without the repeat; longer than kMaxLwpolylinePoints it becomes
    // open chunks over all its points, the last chunk ending on the repeated first point.
    void add_polyline(std::size_t i) {
        const Polyline& pts = doc_.polylines[i].pts;
        const std::size_t n = pts.size();
        if (n < 2) return;
        const auto& a = pts.front();
        const auto& b = pts.back();
        const bool closed = n > 3 && std::hypot(a[0] - b[0], a[1] - b[1]) < 1e-9;
        if (closed && n - 1 <= kMaxLwpolylinePoints) {
            ents_.push_back({Kind::polyline, i, 0, 0, n - 1, true});
            return;
        }
        for (std::size_t start = 0; start + 1 < n;) {
            const std::size_t count = std::min(kMaxLwpolylinePoints, n - start);
            ents_.push_back({Kind::polyline, i, 0, start, count, false});
            start += count - 1;
        }
    }

    void collect_entities() {
        for (std::size_t i = 0; i < doc_.polylines.size(); ++i)
            add_polyline(i);
        for (std::size_t i = 0; i < doc_.arcs.size(); ++i)
            if (doc_.arcs[i].radius > 0.0) ents_.push_back({Kind::arc, i});
        for (std::size_t i = 0; i < doc_.lines3.size(); ++i) ents_.push_back({Kind::line, i});
        for (std::size_t i = 0; i < doc_.faces3.size(); ++i) ents_.push_back({Kind::face, i});
        for (std::size_t i = 0; i < doc_.solids.size(); ++i) ents_.push_back({Kind::solid, i});
        for (std::size_t i = 0; i < doc_.texts.size(); ++i)
            if (doc_.texts[i].height > 0.0 && !dxf_text_string(doc_.texts[i].text).empty())
                ents_.push_back({Kind::text, i});
        for (std::size_t i = 0; i < doc_.acis.size(); ++i) ents_.push_back({Kind::acis, i});
        three_d_ = !doc_.lines3.empty() || !doc_.faces3.empty() || !doc_.acis.empty();
        for (const DxfArc& a : doc_.arcs)
            if (a.radius > 0.0 && !dxf_normal_is_z(a.normal)) three_d_ = true;
        // Every referenced layer exists before handles are assigned.
        for (const Ent& e : ents_) layer_slot(layer_of(e));
    }

    const std::string& layer_of(const Ent& e) const {
        switch (e.kind) {
            case Kind::polyline: return doc_.polylines[e.index].layer;
            case Kind::arc: return doc_.arcs[e.index].layer;
            case Kind::line: return doc_.lines3[e.index].layer;
            case Kind::face: return doc_.faces3[e.index].layer;
            case Kind::solid: return doc_.solids[e.index].layer;
            case Kind::text: return doc_.texts[e.index].layer;
            case Kind::acis: return doc_.acis[e.index].layer;
        }
        static const std::string kNone;
        return kNone;  // unreachable: every Kind is handled above
    }

    void assign_handles() {
        std::uint64_t next = hFirstFree;
        for (std::size_t i = 1; i < layers_.size(); ++i) layers_[i].handle = next++;
        for (Ent& e : ents_) e.handle = next++;
        handseed_ = next;
    }

    void compute_extents() {
        for (const DxfPolyline& pl : doc_.polylines)
            for (const auto& p : pl.pts) ext_.add(p[0], p[1], 0.0);
        for (const DxfArc& a : doc_.arcs) {
            if (!(a.radius > 0.0)) continue;
            P3 lo, hi;
            dxf_arc_box(a, lo, hi);  // WCS box of the OCS circle
            ext_.add(lo[0], lo[1], lo[2]);
            ext_.add(hi[0], hi[1], hi[2]);
        }
        for (const DxfLine3& l : doc_.lines3) {
            ext_.add(l.a[0], l.a[1], l.a[2]);
            ext_.add(l.b[0], l.b[1], l.b[2]);
        }
        for (const DxfFace3& f : doc_.faces3)
            for (const auto& p : f.pts) ext_.add(p[0], p[1], p[2]);
        for (const DxfSolid& s : doc_.solids)
            for (const auto& p : s.pts) ext_.add(p[0], p[1], 0.0);
        for (const DxfText& t : doc_.texts) ext_.add(t.pos[0], t.pos[1], 0.0);
        for (const DxfAcis& a : doc_.acis) add_sat_points(a.sat, ext_);
    }

    std::array<double, 4> limits() const {
        if (doc_.has_limits) return doc_.limits;
        if (ext_.ok) return {ext_.lo[0], ext_.lo[1], ext_.hi[0], ext_.hi[1]};
        return imperial_ ? std::array<double, 4>{0, 0, 12, 9} : std::array<double, 4>{0, 0, 420, 297};
    }

    // Size in document units of a length given in millimetres.
    double doc_mm(double mm) const { return mm / mm_per_unit_; }

    // ------------------------------------------------------------ object encoders

    void put(Obj& o) { objs_.emplace(o.handle, o.finish()); }

    void control(std::uint16_t type, std::uint64_t handle, const std::vector<std::uint64_t>& entries,
                 bool bl_count) {
        Obj o(type, handle);
        o.d.BL(0);  // reactors
        if (bl_count) o.d.BL(static_cast<std::uint32_t>(entries.size()));
        else o.d.BS(static_cast<int>(entries.size()));
        if (type == kDIMSTYLE_CONTROL) o.d.RC(0);  // no additional hard handles
        o.h.H(kSoftPtr, 0);                          // owner: none
        o.h.H(kHardOwner, 0);                        // no extension dictionary
        for (std::uint64_t e : entries) o.h.H(kSoftOwner, e);
        if (type == kBLOCK_CONTROL) {
            o.h.H(kHardOwner, hMsRecord);
            o.h.H(kHardOwner, hPsRecord);
        } else if (type == kLTYPE_CONTROL) {
            o.h.H(kHardOwner, hByBlock);
            o.h.H(kHardOwner, hByLayer);
        }
        put(o);
    }

    void dictionary(std::uint64_t handle, std::uint64_t owner,
                    std::vector<std::pair<std::string, std::uint64_t>> items) {
        std::sort(items.begin(), items.end());
        Obj o(kDICTIONARY, handle);
        object_common(o, owner, owner ? std::vector<std::uint64_t>{owner} : std::vector<std::uint64_t>{});
        o.d.BL(static_cast<std::uint32_t>(items.size()));
        o.d.BS(1);  // cloning: keep existing
        o.d.RC(0);  // not a hard owner
        for (const auto& it : items) o.d.TV(it.first);
        for (const auto& it : items) o.h.H(kSoftOwner, it.second);
        put(o);
    }

    void tables() {
        std::vector<std::uint64_t> layer_handles;
        for (const Layer& l : layers_) layer_handles.push_back(l.handle);
        control(kBLOCK_CONTROL, hBlockCtl, {}, true);
        control(kLAYER_CONTROL, hLayerCtl, layer_handles, true);
        control(kSTYLE_CONTROL, hStyleCtl, {hStyle}, true);
        control(kLTYPE_CONTROL, hLtypeCtl, {hContinuous, hDashed, hCenter}, false);
        control(kVIEW_CONTROL, hViewCtl, {}, true);
        control(kUCS_CONTROL, hUcsCtl, {}, false);
        control(kVPORT_CONTROL, hVportCtl, {hVport}, false);
        control(kAPPID_CONTROL, hAppidCtl, {hAcad}, false);
        control(kDIMSTYLE_CONTROL, hDimstyleCtl, {hDimstyle}, false);
        control(kVX_CONTROL, hVxCtl, {}, false);

        for (const Layer& l : layers_) {
            Obj o(kLAYER, l.handle);
            object_common(o, hLayerCtl, {});
            table_common(o, l.name);
            // On, thawed, unlocked, plotted (16), lineweight Default (31 << 5).
            o.d.BS(16 | (31 << 5));
            o.d.CMC(l.color);
            o.h.H(kHardPtr, hNormal);  // plot style
            o.h.H(kHardPtr, linetype_handle(l.ltype));
            put(o);
        }

        {
            Obj o(kSTYLE, hStyle);
            object_common(o, hStyleCtl, {});
            table_common(o, "Standard");
            o.d.B(false);  // not a shape file
            o.d.B(false);  // not vertical
            o.d.BD(0.0);   // fixed height: none
            o.d.BD(1.0);   // width factor
            o.d.BD(0.0);   // oblique
            o.d.RC(0);     // generation
            o.d.BD(imperial_ ? 0.2 : doc_mm(2.5));  // last height
            o.d.TV("txt");
            o.d.TV("");
            put(o);
        }

        ltype(hByBlock, "ByBlock", "", {});
        ltype(hByLayer, "ByLayer", "", {});
        ltype(hContinuous, "Continuous", "Solid line", {});
        ltype(hDashed, "DASHED", "Dashed __ __ __ __", {doc_mm(kInkDash[0]), -doc_mm(kInkDash[1])});
        ltype(hCenter, "CENTER", "Center ____ _ ____ _ ____",
              {doc_mm(kInkChain[0]), -doc_mm(kInkChain[1]), doc_mm(kInkChain[2]), -doc_mm(kInkChain[3])});

        {
            Obj o(kAPPID, hAcad);
            object_common(o, hAppidCtl, {});
            table_common(o, "ACAD");
            o.d.RC(0);
            put(o);
        }

        dimstyle();
        vport();
    }

    void ltype(std::uint64_t handle, const std::string& name, const std::string& desc,
               const std::vector<double>& dashes) {
        Obj o(kLTYPE, handle);
        object_common(o, hLtypeCtl, {});
        table_common(o, name);
        double total = 0.0;
        for (double v : dashes) total += std::fabs(v);
        o.d.TV(desc);
        o.d.BD(total);
        o.d.RC('A');  // alignment
        o.d.RC(static_cast<std::uint8_t>(dashes.size()));
        for (double v : dashes) {
            o.d.BD(v);
            o.d.BS(0);       // complex shape code
            o.h.H(kHardPtr, 0);  // shape style
            o.d.RD(0.0);     // x offset
            o.d.RD(0.0);     // y offset
            o.d.BD(1.0);     // scale
            o.d.BD(0.0);     // rotation
            o.d.BS(0);       // shape flag
        }
        for (int i = 0; i < 256; ++i) o.d.RC(0);  // text area of complex linetypes
        put(o);
    }

    // DIMSTYLE values: imperial "Standard" or metric ISO-25 sizes in document units.
    struct DimDefaults {
        double asz, exo, dli, exe, txt, cen, gap, altf;
        int dec, zin, lunit, dsep;
    };
    DimDefaults dim_defaults() const {
        if (imperial_) return {0.18, 0.0625, 0.38, 0.18, 0.18, 0.09, 0.09, 25.4, 4, 0, 2, '.'};
        return {doc_mm(2.5), doc_mm(0.625), doc_mm(3.75), doc_mm(1.25), doc_mm(2.5), doc_mm(2.5),
                doc_mm(0.625), 0.0393700787, 2, 8, 2, ','};
    }

    void dimstyle() {
        const DimDefaults dd = dim_defaults();
        Obj o(kDIMSTYLE, hDimstyle);
        object_common(o, hDimstyleCtl, {});
        table_common(o, "Standard");
        auto& d = o.d;
        d.TV("");          // DIMPOST
        d.TV("");          // DIMAPOST
        d.BD(1.0);         // DIMSCALE
        d.BD(dd.asz);      // DIMASZ
        d.BD(dd.exo);      // DIMEXO
        d.BD(dd.dli);      // DIMDLI
        d.BD(dd.exe);      // DIMEXE
        d.BD(0.0);         // DIMRND
        d.BD(0.0);         // DIMDLE
        d.BD(0.0);         // DIMTP
        d.BD(0.0);         // DIMTM
        d.B(false);        // DIMTOL
        d.B(false);        // DIMLIM
        d.B(true);         // DIMTIH
        d.B(true);         // DIMTOH
        d.B(false);        // DIMSE1
        d.B(false);        // DIMSE2
        d.BS(0);           // DIMTAD
        d.BS(dd.zin);      // DIMZIN
        d.BS(0);           // DIMAZIN
        d.BD(dd.txt);      // DIMTXT
        d.BD(dd.cen);      // DIMCEN
        d.BD(0.0);         // DIMTSZ
        d.BD(dd.altf);     // DIMALTF
        d.BD(1.0);         // DIMLFAC
        d.BD(0.0);         // DIMTVP
        d.BD(1.0);         // DIMTFAC
        d.BD(dd.gap);      // DIMGAP
        d.BD(0.0);         // DIMALTRND
        d.B(false);        // DIMALT
        d.BS(2);           // DIMALTD
        d.B(false);        // DIMTOFL
        d.B(false);        // DIMSAH
        d.B(false);        // DIMTIX
        d.B(false);        // DIMSOXD
        d.CMC(0);          // DIMCLRD ByBlock
        d.CMC(0);          // DIMCLRE
        d.CMC(0);          // DIMCLRT
        d.BS(0);           // DIMADEC
        d.BS(dd.dec);      // DIMDEC
        d.BS(dd.dec);      // DIMTDEC
        d.BS(2);           // DIMALTU
        d.BS(2);           // DIMALTTD
        d.BS(0);           // DIMAUNIT
        d.BS(0);           // DIMFRAC
        d.BS(dd.lunit);    // DIMLUNIT
        d.BS(dd.dsep);     // DIMDSEP
        d.BS(0);           // DIMTMOVE
        d.BS(0);           // DIMJUST
        d.B(false);        // DIMSD1
        d.B(false);        // DIMSD2
        d.BS(1);           // DIMTOLJ
        d.BS(0);           // DIMTZIN
        d.BS(0);           // DIMALTZ
        d.BS(0);           // DIMALTTZ
        d.B(false);        // DIMUPT
        d.BS(3);           // DIMATFIT
        d.BS(-2);          // DIMLWD ByBlock
        d.BS(-2);          // DIMLWE ByBlock
        d.B(false);        // flag bit 0
        o.h.H(kHardPtr, hStyle);  // DIMTXSTY
        for (int i = 0; i < 4; ++i) o.h.H(kHardPtr, 0);  // DIMLDRBLK, DIMBLK, DIMBLK1, DIMBLK2
        put(o);
    }

    void vport() {
        Obj o(kVPORT, hVport);
        object_common(o, hVportCtl, {});
        table_common(o, "*Active");
        auto& d = o.d;
        constexpr double kAspect = 1.6;
        double vsize = 1.0;
        P3 target{0, 0, 0}, dir{0, 0, 1};
        double cx = 0.0, cy = 0.0;
        if (ext_.ok) {
            const double w = ext_.hi[0] - ext_.lo[0], hgt = ext_.hi[1] - ext_.lo[1];
            if (three_d_) {
                // South-east isometric view centred on the model.
                const double dz = ext_.hi[2] - ext_.lo[2];
                target = {(ext_.lo[0] + ext_.hi[0]) / 2, (ext_.lo[1] + ext_.hi[1]) / 2,
                          (ext_.lo[2] + ext_.hi[2]) / 2};
                dir = {1, -1, 1};
                vsize = std::sqrt(w * w + hgt * hgt + dz * dz) * 1.1;
            } else {
                cx = (ext_.lo[0] + ext_.hi[0]) / 2;
                cy = (ext_.lo[1] + ext_.hi[1]) / 2;
                vsize = std::max(hgt, w / kAspect) * 1.1;
            }
            if (!(vsize > 0.0)) vsize = 1.0;
        }
        const double grid = imperial_ ? 0.5 : doc_mm(10.0);
        d.BD(vsize);             // VIEWSIZE
        d.BD(vsize * kAspect);   // view width
        d.RD2(cx, cy);           // VIEWCTR
        d.BD3(target);           // view target
        d.BD3(dir);              // VIEWDIR
        d.BD(0.0);               // VIEWTWIST
        d.BD(50.0);              // LENSLENGTH
        d.BD(0.0);               // FRONTZ
        d.BD(0.0);               // BACKZ
        d.bits(0, 4);            // VIEWMODE
        d.RC(0);                 // render mode
        d.RD2(0.0, 0.0);         // lower left
        d.RD2(1.0, 1.0);         // upper right
        d.B(false);              // UCSFOLLOW
        d.BS(1000);              // circle zoom
        d.B(true);               // FASTZOOM
        d.BB(3);                 // UCSICON on, at origin
        d.B(false);              // GRIDMODE
        d.RD2(grid, grid);       // GRIDUNIT
        d.B(false);              // SNAPMODE
        d.B(false);              // SNAPSTYLE
        d.BS(0);                 // SNAPISOPAIR
        d.BD(0.0);               // SNAPANG
        d.RD2(0.0, 0.0);         // SNAPBASE
        d.RD2(grid, grid);       // SNAPUNIT
        d.B(false);              // UCS at origin
        d.B(true);               // UCSVP
        d.BD3(0, 0, 0);          // UCS origin
        d.BD3(1, 0, 0);          // UCS x
        d.BD3(0, 1, 0);          // UCS y
        d.BD(0.0);               // UCS elevation
        d.BS(0);                 // UCSORTHOVIEW
        o.h.H(kHardPtr, 0);      // named UCS
        o.h.H(kHardPtr, 0);      // base UCS
        put(o);
    }

    void block_record(std::uint64_t handle, const std::string& name, std::uint64_t block,
                      std::uint64_t first, std::uint64_t last, std::uint64_t endblk,
                      std::uint64_t layout) {
        Obj o(kBLOCK_HEADER, handle);
        object_common(o, hBlockCtl, {});
        table_common(o, name);
        o.d.B(false);  // anonymous
        o.d.B(false);  // has attributes
        o.d.B(false);  // is xref
        o.d.B(false);  // xref overlaid
        o.d.B(false);  // xref loaded
        o.d.BD3(0, 0, 0);  // base point
        o.d.TV("");    // xref path
        o.d.RC(0);     // no INSERTs reference it
        o.d.TV("");    // description
        o.d.BL(0);     // no preview
        o.h.H(kHardOwner, block);
        o.h.H(kSoftPtr, first);
        o.h.H(kSoftPtr, last);
        o.h.H(kHardOwner, endblk);
        o.h.H(kHardPtr, layout);
        put(o);
    }

    void block_entities(std::uint64_t block, std::uint64_t endblk, int entmode, const std::string& name) {
        EntityLinks l;
        l.entmode = entmode;
        l.layer = hLayer0;
        {
            Obj o(kBLOCK, block);
            entity_common(o, l);
            o.d.TV(name);
            put(o);
        }
        {
            Obj o(kENDBLK, endblk);
            entity_common(o, l);
            put(o);
        }
    }

    struct PlotSettings {
        std::string printer, paper, media;
        int flags;
        double margins[4];
        double paper_w, paper_h;
        int paper_unit, rotation, plot_type;
        double paper_units, drawing_units;
        int scale_type;
        double scale_factor;
    };

    void layout(std::uint64_t handle, const std::string& name, int tab, const PlotSettings& ps,
                std::uint64_t record) {
        const std::array<double, 4> lim = imperial_ ? std::array<double, 4>{0, 0, 12, 9}
                                                    : std::array<double, 4>{0, 0, 420, 297};
        Obj o(kLAYOUT, handle);
        object_common(o, hLayoutDict, {hLayoutDict});
        auto& d = o.d;
        d.TV(ps.printer);
        d.TV(ps.paper);
        d.BS(ps.flags);
        for (double m : ps.margins) d.BD(m);
        d.BD(ps.paper_w);
        d.BD(ps.paper_h);
        d.TV(ps.media);
        d.BD(0.0); d.BD(0.0);    // plot origin
        d.BS(ps.paper_unit);
        d.BS(ps.rotation);
        d.BS(ps.plot_type);
        d.BD(0.0); d.BD(0.0);    // window lower left
        d.BD(0.0); d.BD(0.0);    // window upper right
        d.TV("");                // plot view name
        d.BD(ps.paper_units);
        d.BD(ps.drawing_units);
        d.TV("");                // current style sheet
        d.BS(ps.scale_type);
        d.BD(ps.scale_factor);
        d.BD(0.0); d.BD(0.0);    // paper image origin
        d.TV(name);
        d.BS(tab);
        d.BS(1);                 // layout flags: PSLTSCALE
        d.BD3(0, 0, 0);          // INSBASE
        d.RD2(lim[0], lim[1]);   // LIMMIN
        d.RD2(lim[2], lim[3]);   // LIMMAX
        d.BD3(0, 0, 0);          // UCS origin
        d.BD3(1, 0, 0);          // UCS x
        d.BD3(0, 1, 0);          // UCS y
        d.BD(0.0);               // UCS elevation
        d.BS(0);                 // UCSORTHOVIEW
        d.BD3(0, 0, 0);          // EXTMIN
        d.BD3(0, 0, 0);          // EXTMAX
        o.h.H(kSoftPtr, record);  // block record
        o.h.H(kSoftPtr, 0);       // active viewport
        o.h.H(kHardPtr, 0);       // base UCS
        o.h.H(kHardPtr, 0);       // named UCS
        put(o);
    }

    void dictionaries() {
        dictionary(hNod, 0,
                   {{"ACAD_GROUP", hGroupDict},
                    {"ACAD_LAYOUT", hLayoutDict},
                    {"ACAD_MLINESTYLE", hMlineDict},
                    {"ACAD_PLOTSETTINGS", hPlotSettingsDict},
                    {"ACAD_PLOTSTYLENAME", hPlotStyleDict}});
        dictionary(hGroupDict, hNod, {});
        dictionary(hMlineDict, hNod, {{"Standard", hMline}});
        dictionary(hPlotSettingsDict, hNod, {});
        dictionary(hLayoutDict, hNod, {{"Layout1", hLayout1}, {"Model", hLayoutModel}});
        {
            Obj o(kDICTIONARYWDFLT, hPlotStyleDict);
            object_common(o, hNod, {hNod});
            o.d.BL(1);
            o.d.BS(1);
            o.d.RC(0);
            o.d.TV("Normal");
            o.h.H(kSoftOwner, hNormal);
            o.h.H(kHardPtr, hNormal);  // default entry
            put(o);
        }
        {
            Obj o(kPLACEHOLDER, hNormal);
            object_common(o, hPlotStyleDict, {hPlotStyleDict});
            put(o);
        }
        {
            Obj o(kMLINESTYLE, hMline);
            object_common(o, hMlineDict, {hMlineDict});
            o.d.TV("STANDARD");
            o.d.TV("");
            o.d.BS(0);                  // flags
            o.d.CMC(kColorByLayer);     // fill colour
            o.d.BD(3.14159265358979323846 / 2);  // start angle
            o.d.BD(3.14159265358979323846 / 2);  // end angle
            o.d.RC(2);
            for (double off : {0.5, -0.5}) {
                o.d.BD(off);
                o.d.CMC(kColorByLayer);
                o.d.BS(32767);          // linetype ByLayer
            }
            put(o);
        }
        const bool imp = imperial_;
        const PlotSettings model{"",
                                 "none_device",
                                 imp ? "ANSI_A_(8.50_x_11.00_Inches)" : "ISO_A4_(210.00_x_297.00_MM)",
                                 0x2EB0,
                                 {6.35, 19.05, 6.35, 19.05},
                                 imp ? 215.9 : 210.0,
                                 imp ? 279.4 : 297.0,
                                 imp ? 0 : 1,
                                 1,
                                 0,
                                 1.0,
                                 1.0,
                                 0,
                                 1.0};
        const PlotSettings paper{"", "None", "", 0x2B0, {0, 0, 0, 0}, 0.0, 0.0, imp ? 0 : 1, 0, 5,
                                 1.0, 1.0, 16, 1.0};
        layout(hLayoutModel, "Model", 0, model, hMsRecord);
        layout(hLayout1, "Layout1", 1, paper, hPsRecord);
    }

    void entities() {
        for (std::size_t i = 0; i < ents_.size(); ++i) {
            const Ent& e = ents_[i];
            EntityLinks l;
            l.entmode = 2;
            l.layer = layers_[by_doc_.at(layer_of(e))].handle;
            l.prev = i > 0 ? ents_[i - 1].handle : 0;
            l.next = i + 1 < ents_.size() ? ents_[i + 1].handle : 0;
            switch (e.kind) {
                case Kind::polyline: polyline(e, l); break;
                case Kind::arc: arc(e, l); break;
                case Kind::line: line(e, l); break;
                case Kind::face: face(e, l); break;
                case Kind::solid: solid(e, l); break;
                case Kind::text: text(e, l); break;
                case Kind::acis: acis(e, l); break;
            }
        }
    }

    void set_ltype(EntityLinks& l, const std::string& name, const std::string& what) {
        const std::string u = upper(name);
        if (u.empty() || u == "BYLAYER") return;
        if (u == "BYBLOCK") { l.ltype_flags = 1; return; }
        l.ltype_flags = 3;
        l.ltype = linetype_handle(linetype_index(name, what));
    }

    void polyline(const Ent& e, EntityLinks l) {
        const DxfPolyline& pl = doc_.polylines[e.index];
        const std::string what = "polyline on layer '" + pl.layer + "'";
        for (std::size_t i = e.first; i < e.first + e.count; ++i) {
            need_finite(pl.pts[i][0], what);
            need_finite(pl.pts[i][1], what);
        }
        need_finite(pl.width, what);
        set_ltype(l, pl.ltype, what);
        Obj o(kLWPOLYLINE, e.handle);
        entity_common(o, l);
        int flag = e.closed ? 512 : 0;
        if (pl.width > 0.0) flag |= 4;
        o.d.BS(flag);
        if (flag & 4) o.d.BD(pl.width);
        o.d.BL(static_cast<std::uint32_t>(e.count));
        const auto* p = pl.pts.data() + e.first;
        o.d.RD2(p[0][0], p[0][1]);
        for (std::size_t i = 1; i < e.count; ++i) {
            o.d.DD(p[i][0], p[i - 1][0]);
            o.d.DD(p[i][1], p[i - 1][1]);
        }
        put(o);
    }

    void arc(const Ent& e, EntityLinks l) {
        const DxfArc& a = doc_.arcs[e.index];
        const std::string what = std::string(a.circle ? "circle" : "arc") + " on layer '" + a.layer + "'";
        for (double v : {a.center[0], a.center[1], a.z, a.radius, a.start_deg, a.end_deg}) need_finite(v, what);
        // Centre and angles are OCS values of the extrusion (Arbitrary Axis Algorithm).
        P3 normal{0.0, 0.0, 1.0};
        if (!dxf_normal_is_z(a.normal) && !dxf_unit_normal(a.normal, normal))
            throw std::runtime_error("write_dwg: " + what + " has an invalid extrusion");
        set_ltype(l, a.ltype, what);
        Obj o(a.circle ? kCIRCLE : kARC, e.handle);
        entity_common(o, l);
        o.d.BD3(a.center[0], a.center[1], a.z);
        o.d.BD(a.radius);
        o.d.BT(0.0);
        o.d.BE(normal);
        if (!a.circle) {
            o.d.BD(deg2rad(a.start_deg));
            o.d.BD(deg2rad(a.end_deg));
        }
        put(o);
    }

    void line(const Ent& e, const EntityLinks& l) {
        const DxfLine3& ln = doc_.lines3[e.index];
        const std::string what = "line on layer '" + ln.layer + "'";
        for (int k = 0; k < 3; ++k) { need_finite(ln.a[k], what); need_finite(ln.b[k], what); }
        Obj o(kLINE, e.handle);
        entity_common(o, l);
        const bool z_zero = ln.a[2] == 0.0 && ln.b[2] == 0.0;
        o.d.B(z_zero);
        o.d.RD(ln.a[0]);
        o.d.DD(ln.b[0], ln.a[0]);
        o.d.RD(ln.a[1]);
        o.d.DD(ln.b[1], ln.a[1]);
        if (!z_zero) {
            o.d.RD(ln.a[2]);
            o.d.DD(ln.b[2], ln.a[2]);
        }
        o.d.BT(0.0);
        o.d.BE({0, 0, 1});
        put(o);
    }

    void face(const Ent& e, const EntityLinks& l) {
        const DxfFace3& f = doc_.faces3[e.index];
        const std::string what = "3D face on layer '" + f.layer + "'";
        for (const auto& p : f.pts)
            for (double v : p) need_finite(v, what);
        // A triangle repeats its third corner as the fourth; the degenerate third edge is
        // always invisible and the closing edge (corner 3 -> 1) takes bit 3.
        const int invis = (f.hidden_edges & 0x3) | 0x4 | ((f.hidden_edges & 0x4) ? 0x8 : 0);
        Obj o(k3DFACE, e.handle);
        entity_common(o, l);
        const auto& c1 = f.pts[0];
        const auto& c2 = f.pts[1];
        const auto& c3 = f.pts[2];
        o.d.B(false);                 // has invisibility flags
        const bool z_zero = c1[2] == 0.0;
        o.d.B(z_zero);
        o.d.RD(c1[0]);
        o.d.RD(c1[1]);
        if (!z_zero) o.d.RD(c1[2]);
        for (int k = 0; k < 3; ++k) o.d.DD(c2[k], c1[k]);
        for (int k = 0; k < 3; ++k) o.d.DD(c3[k], c2[k]);
        for (int k = 0; k < 3; ++k) o.d.DD(c3[k], c3[k]);  // corner 4 = corner 3
        o.d.BS(invis);
        put(o);
    }

    void solid(const Ent& e, const EntityLinks& l) {
        const DxfSolid& s = doc_.solids[e.index];
        const std::string what = "solid on layer '" + s.layer + "'";
        for (const auto& p : s.pts) { need_finite(p[0], what); need_finite(p[1], what); }
        Obj o(kSOLID, e.handle);
        entity_common(o, l);
        o.d.BT(0.0);
        o.d.BD(0.0);  // elevation
        o.d.RD2(s.pts[0][0], s.pts[0][1]);
        o.d.RD2(s.pts[1][0], s.pts[1][1]);
        o.d.RD2(s.pts[2][0], s.pts[2][1]);
        o.d.RD2(s.pts[2][0], s.pts[2][1]);
        o.d.BE({0, 0, 1});
        put(o);
    }

    void text(const Ent& e, const EntityLinks& l) {
        const DxfText& t = doc_.texts[e.index];
        const std::string what = "text on layer '" + t.layer + "'";
        need_finite(t.pos[0], what);
        need_finite(t.pos[1], what);
        need_finite(t.height, what);
        if (t.halign < 0 || t.halign > 2 || !(t.valign == 0 || t.valign == 2 || t.valign == 3))
            throw std::runtime_error("write_dwg: text on layer '" + t.layer + "' has alignment (" +
                                     std::to_string(t.halign) + ", " + std::to_string(t.valign) +
                                     "); TEXT takes halign 0/1/2 and valign 0/2/3");
        const std::string str = dxf_text_string(t.text);
        if (str.size() > 0xFFFF)
            throw std::runtime_error("write_dwg: text on layer '" + t.layer + "' is " +
                                     std::to_string(str.size()) + " bytes; DWG text holds at most 65535");
        const bool aligned = t.halign != 0 || t.valign != 0;
        // The alignment point (11) is `pos`; the insertion point (10) is the left baseline
        // AutoCAD would derive, estimated with the width the drawing layout uses, so
        // readers that ignore alignment still place the text where it is drawn.
        double ix = t.pos[0], iy = t.pos[1];
        if (aligned) {
            const double w = annot_text_width(t.text, t.height);
            ix -= t.halign == 1 ? w * 0.5 : t.halign == 2 ? w : 0.0;
            iy -= t.valign == 2 ? t.height * 0.5 : t.valign == 3 ? t.height : 0.0;
        }
        // Data flags: 0x01 elevation 0, 0x02 no alignment point, 0x04 oblique 0,
        // 0x08 rotation 0, 0x10 width factor 1, 0x20 generation 0, 0x40 halign 0,
        // 0x80 valign 0.
        std::uint8_t flags = 0x01 | 0x04 | 0x08 | 0x10 | 0x20;
        if (!aligned) flags |= 0x02;
        if (t.halign == 0) flags |= 0x40;
        if (t.valign == 0) flags |= 0x80;
        Obj o(kTEXT, e.handle);
        entity_common(o, l);
        o.d.RC(flags);
        o.d.RD2(ix, iy);
        if (aligned) {
            o.d.DD(t.pos[0], ix);
            o.d.DD(t.pos[1], iy);
        }
        o.d.BE({0, 0, 1});
        o.d.BT(0.0);
        o.d.RD(t.height);
        o.d.TV(str);
        if (t.halign != 0) o.d.BS(t.halign);
        if (t.valign != 0) o.d.BS(t.valign);
        o.h.H(kHardPtr, hStyle);
        put(o);
    }

    void acis(const Ent& e, const EntityLinks& l) {
        const DxfAcis& a = doc_.acis[e.index];
        const std::string what = std::string(a.solid ? "3DSOLID" : "BODY") + " on layer '" + a.layer + "'";
        if (a.sat.empty()) throw std::runtime_error("write_dwg: " + what + " has no SAT data");
        int version = 0;
        {
            std::istringstream head(a.sat);
            if (!(head >> version) || version <= 0)
                throw std::runtime_error("write_dwg: " + what + " SAT header has no version number");
        }
        if (version > 400)
            throw std::runtime_error("write_dwg: " + what + " is SAT version " + std::to_string(version) +
                                     "; an R2000 DWG embeds ACIS 4.00 (version <= 400)");
        if (a.sat.find("End-of-ACIS-data") == std::string::npos)
            throw std::runtime_error("write_dwg: " + what + " SAT lacks its End-of-ACIS-data terminator");
        // R2000 modeler geometry, version 1: the SAT text in blocks, each byte above 32
        // stored as 159 - byte (AutoCAD's obfuscation). Only 7-bit text survives that map.
        // Lines end in CR LF inside the DWG, as AutoCAD stores them.
        std::string enc;
        enc.reserve(a.sat.size() + a.sat.size() / 32);
        for (std::size_t i = 0; i < a.sat.size(); ++i) {
            const auto c = static_cast<std::uint8_t>(a.sat[i]);
            if (c > 126)
                throw std::runtime_error("write_dwg: " + what + " SAT has a non-ASCII byte at offset " +
                                         std::to_string(i));
            if (c == '\n' && (i == 0 || a.sat[i - 1] != '\r')) enc.push_back('\r');
            enc.push_back(static_cast<char>(c <= 32 ? c : 159 - c));
        }
        Obj o(a.solid ? k3DSOLID : kBODY, e.handle);
        entity_common(o, l);
        o.d.B(false);   // ACIS data present
        o.d.B(true);    // unknown bit, set by AutoCAD
        o.d.BS(1);      // modeler format version 1: encrypted SAT text
        constexpr std::size_t kBlock = 4096;
        for (std::size_t at = 0; at < enc.size(); at += kBlock) {
            const std::size_t n = std::min(kBlock, enc.size() - at);
            o.d.BL(static_cast<std::uint32_t>(n));
            for (std::size_t i = 0; i < n; ++i) o.d.RC(static_cast<std::uint8_t>(enc[at + i]));
        }
        o.d.BL(0);      // end of the SAT blocks
        o.d.B(false);   // no cached wireframe; AutoCAD regenerates isolines from the body
        o.d.B(false);   // ACIS empty bit
        put(o);
    }

    void objects() {
        tables();
        dictionaries();
        const std::uint64_t first = ents_.front().handle, last = ents_.back().handle;
        block_record(hMsRecord, "*Model_Space", hMsBlock, first, last, hMsEndblk, hLayoutModel);
        block_entities(hMsBlock, hMsEndblk, 2, "*Model_Space");
        block_record(hPsRecord, "*Paper_Space", hPsBlock, 0, 0, hPsEndblk, hLayout1);
        block_entities(hPsBlock, hPsEndblk, 1, "*Paper_Space");
        entities();
    }

    // ------------------------------------------------------------ sections

    Bits header_variables(std::uint32_t days, std::uint32_t ms) {
        Bits d;
        const DimDefaults dd = dim_defaults();
        const std::array<double, 4> lim = limits();
        const std::array<double, 4> plim = imperial_ ? std::array<double, 4>{0, 0, 12, 9}
                                                     : std::array<double, 4>{0, 0, 420, 297};
        const P3 lo = ext_.ok ? ext_.lo : P3{0, 0, 0};
        const P3 hi = ext_.ok ? ext_.hi : P3{0, 0, 0};
        d.BD(412148564080.0);   // unit conversion ratios
        d.BD(1.0);
        d.BD(1.0);
        d.BD(1.0);
        d.TV("m");
        d.TV("");
        d.TV("");
        d.TV("");
        d.BL(0);
        d.BL(0);
        d.H(kHardPtr, 0);       // current VX table record
        for (bool v : {true, true, false, false, true, true, false, true, false, true, false, false,
                       false, false, true, true, false, true, false, false})
            d.B(v);  // DIMASO DIMSHO PLINEGEN ORTHOMODE REGENMODE FILLMODE QTEXTMODE PSLTSCALE
                     // LIMCHECK USRTIMER SKPOLY ANGDIR SPLFRAME MIRRTEXT WORLDVIEW TILEMODE
                     // PLIMCHECK VISRETAIN DISPSILH PELLIPSE
        d.BS(1);       // PROXYGRAPHICS
        d.BS(3020);    // TREEDEPTH
        d.BS(2);       // LUNITS decimal
        d.BS(4);       // LUPREC
        d.BS(0);       // AUNITS
        d.BS(0);       // AUPREC
        d.BS(1);       // ATTMODE
        d.BS(0);       // PDMODE
        for (int i = 0; i < 5; ++i) d.BS(0);  // USERI1-5
        d.BS(8);       // SPLINESEGS
        d.BS(6);       // SURFU
        d.BS(6);       // SURFV
        d.BS(6);       // SURFTYPE
        d.BS(6);       // SURFTAB1
        d.BS(6);       // SURFTAB2
        d.BS(6);       // SPLINETYPE
        d.BS(3);       // SHADEDGE
        d.BS(70);      // SHADEDIF
        d.BS(0);       // UNITMODE
        d.BS(64);      // MAXACTVP
        d.BS(4);       // ISOLINES
        d.BS(0);       // CMLJUST
        d.BS(50);      // TEXTQLTY
        d.BD(1.0);     // LTSCALE
        d.BD(imperial_ ? 0.2 : doc_mm(2.5));   // TEXTSIZE
        d.BD(imperial_ ? 0.05 : doc_mm(1.0));  // TRACEWID
        d.BD(imperial_ ? 0.1 : doc_mm(1.0));   // SKETCHINC
        d.BD(0.0);     // FILLETRAD
        d.BD(0.0);     // THICKNESS
        d.BD(0.0);     // ANGBASE
        d.BD(0.0);     // PDSIZE
        d.BD(0.0);     // PLINEWID
        for (int i = 0; i < 5; ++i) d.BD(0.0);  // USERR1-5
        for (int i = 0; i < 4; ++i) d.BD(0.0);  // CHAMFERA-D
        d.BD(0.5);     // FACETRES
        d.BD(imperial_ ? 1.0 : doc_mm(20.0));  // CMLSCALE
        d.BD(1.0);     // CELTSCALE
        d.TV("acad");  // MENU
        d.TIMEBLL(days, ms);  // TDCREATE
        d.TIMEBLL(days, ms);  // TDUPDATE
        d.TIMEBLL(0, 0);      // TDINDWG
        d.TIMEBLL(0, 0);      // TDUSRTIMER
        d.CMC(kColorByLayer); // CECOLOR
        d.H(0, handseed_);    // HANDSEED
        d.H(kHardPtr, hLayer0);     // CLAYER
        d.H(kHardPtr, hStyle);      // TEXTSTYLE
        d.H(kHardPtr, hByLayer);    // CELTYPE
        d.H(kHardPtr, hDimstyle);   // DIMSTYLE
        d.H(kHardPtr, hMline);      // CMLSTYLE
        d.BD(0.0);                  // PSVPSCALE
        d.BD3(0, 0, 0);             // PINSBASE
        d.BD3(0, 0, 0);             // PEXTMIN
        d.BD3(0, 0, 0);             // PEXTMAX
        d.RD2(plim[0], plim[1]);    // PLIMMIN
        d.RD2(plim[2], plim[3]);    // PLIMMAX
        d.BD(0.0);                  // PELEVATION
        d.BD3(0, 0, 0);             // PUCSORG
        d.BD3(1, 0, 0);             // PUCSXDIR
        d.BD3(0, 1, 0);             // PUCSYDIR
        d.H(kHardPtr, 0);           // PUCSNAME
        d.H(kHardPtr, 0);           // PUCSORTHOREF
        d.BS(0);                    // PUCSORTHOVIEW
        d.H(kHardPtr, 0);           // PUCSBASE
        for (int i = 0; i < 6; ++i) d.BD3(0, 0, 0);  // PUCSORGTOP..BACK
        d.BD3(0, 0, 0);             // INSBASE
        d.BD3(lo);                  // EXTMIN
        d.BD3(hi);                  // EXTMAX
        d.RD2(lim[0], lim[1]);      // LIMMIN
        d.RD2(lim[2], lim[3]);      // LIMMAX
        d.BD(0.0);                  // ELEVATION
        d.BD3(0, 0, 0);             // UCSORG
        d.BD3(1, 0, 0);             // UCSXDIR
        d.BD3(0, 1, 0);             // UCSYDIR
        d.H(kHardPtr, 0);           // UCSNAME
        d.H(kHardPtr, 0);           // UCSORTHOREF
        d.BS(0);                    // UCSORTHOVIEW
        d.H(kHardPtr, 0);           // UCSBASE
        for (int i = 0; i < 6; ++i) d.BD3(0, 0, 0);  // UCSORGTOP..BACK
        d.TV("");                   // DIMPOST
        d.TV("");                   // DIMAPOST
        d.BD(1.0);                  // DIMSCALE
        d.BD(dd.asz);               // DIMASZ
        d.BD(dd.exo);               // DIMEXO
        d.BD(dd.dli);               // DIMDLI
        d.BD(dd.exe);               // DIMEXE
        d.BD(0.0);                  // DIMRND
        d.BD(0.0);                  // DIMDLE
        d.BD(0.0);                  // DIMTP
        d.BD(0.0);                  // DIMTM
        d.B(false);                 // DIMTOL
        d.B(false);                 // DIMLIM
        d.B(true);                  // DIMTIH
        d.B(true);                  // DIMTOH
        d.B(false);                 // DIMSE1
        d.B(false);                 // DIMSE2
        d.BS(0);                    // DIMTAD
        d.BS(dd.zin);               // DIMZIN
        d.BS(0);                    // DIMAZIN
        d.BD(dd.txt);               // DIMTXT
        d.BD(dd.cen);               // DIMCEN
        d.BD(0.0);                  // DIMTSZ
        d.BD(dd.altf);              // DIMALTF
        d.BD(1.0);                  // DIMLFAC
        d.BD(0.0);                  // DIMTVP
        d.BD(1.0);                  // DIMTFAC
        d.BD(dd.gap);               // DIMGAP
        d.BD(0.0);                  // DIMALTRND
        d.B(false);                 // DIMALT
        d.BS(2);                    // DIMALTD
        d.B(false);                 // DIMTOFL
        d.B(false);                 // DIMSAH
        d.B(false);                 // DIMTIX
        d.B(false);                 // DIMSOXD
        d.CMC(0);                   // DIMCLRD
        d.CMC(0);                   // DIMCLRE
        d.CMC(0);                   // DIMCLRT
        d.BS(0);                    // DIMADEC
        d.BS(dd.dec);               // DIMDEC
        d.BS(dd.dec);               // DIMTDEC
        d.BS(2);                    // DIMALTU
        d.BS(2);                    // DIMALTTD
        d.BS(0);                    // DIMAUNIT
        d.BS(0);                    // DIMFRAC
        d.BS(dd.lunit);             // DIMLUNIT
        d.BS(dd.dsep);              // DIMDSEP
        d.BS(0);                    // DIMTMOVE
        d.BS(0);                    // DIMJUST
        d.B(false);                 // DIMSD1
        d.B(false);                 // DIMSD2
        d.BS(1);                    // DIMTOLJ
        d.BS(0);                    // DIMTZIN
        d.BS(0);                    // DIMALTZ
        d.BS(0);                    // DIMALTTZ
        d.B(false);                 // DIMUPT
        d.BS(3);                    // DIMATFIT
        d.H(kHardPtr, hStyle);      // DIMTXSTY
        for (int i = 0; i < 4; ++i) d.H(kHardPtr, 0);  // DIMLDRBLK DIMBLK DIMBLK1 DIMBLK2
        d.BS(-2);                   // DIMLWD
        d.BS(-2);                   // DIMLWE
        for (std::uint64_t h : {hBlockCtl, hLayerCtl, hStyleCtl, hLtypeCtl, hViewCtl, hUcsCtl,
                                hVportCtl, hAppidCtl, hDimstyleCtl, hVxCtl})
            d.H(kHardOwner, h);
        d.H(kHardPtr, hGroupDict);  // DICTIONARY_ACAD_GROUP
        d.H(kHardPtr, hMlineDict);  // DICTIONARY_ACAD_MLINESTYLE
        d.H(kHardOwner, hNod);      // DICTIONARY_NAMED_OBJECT
        d.BS(1);                    // TSTACKALIGN
        d.BS(70);                   // TSTACKSIZE
        d.TV("");                   // HYPERLINKBASE
        d.TV("");                   // STYLESHEET
        d.H(kHardPtr, hLayoutDict);        // DICTIONARY_LAYOUT
        d.H(kHardPtr, hPlotSettingsDict);  // DICTIONARY_PLOTSETTINGS
        d.H(kHardPtr, hPlotStyleDict);     // DICTIONARY_PLOTSTYLENAME
        // FLAGS: CELWEIGHT ByLayer (0x1D), LWDISPLAY off (0x200), EXTNAMES (0x800),
        // PSTYLEMODE colour-dependent (0x2000).
        d.BL(0x1D | 0x200 | 0x800 | 0x2000);
        d.BS(doc_.insunits);        // INSUNITS
        d.BS(0);                    // CEPSNTYPE ByLayer
        d.TV(make_guid(rng_));      // FINGERPRINTGUID
        d.TV(make_guid(rng_));      // VERSIONGUID
        d.H(kHardPtr, hPsRecord);   // BLOCK_RECORD_PSPACE
        d.H(kHardPtr, hMsRecord);   // BLOCK_RECORD_MSPACE
        d.H(kHardPtr, hByLayer);    // LTYPE_BYLAYER
        d.H(kHardPtr, hByBlock);    // LTYPE_BYBLOCK
        d.H(kHardPtr, hContinuous); // LTYPE_CONTINUOUS
        for (int i = 0; i < 4; ++i) d.BS(0xFFFF);  // R14+ trailing words
        return d;
    }

    Bits classes() {
        struct Cls { int num; const char* cpp; const char* dxf; };
        static const Cls kClasses[] = {
            {kDICTIONARYWDFLT, "AcDbDictionaryWithDefault", "ACDBDICTIONARYWDFLT"},
            {kPLACEHOLDER, "AcDbPlaceHolder", "ACDBPLACEHOLDER"},
            {kLAYOUT, "AcDbLayout", "LAYOUT"},
        };
        Bits d;
        for (const Cls& c : kClasses) {
            d.BS(c.num);
            d.BS(0);  // proxy flags
            d.TV("ObjectDBX Classes");
            d.TV(c.cpp);
            d.TV(c.dxf);
            d.B(false);  // was a zombie
            d.BS(0x1F3); // objects (0x1F2 would be entities)
        }
        return d;
    }

    // sentinel | RL size | data | CRC(size + data) | sentinel
    static void framed(Out& out, const std::uint8_t (&begin)[16], const Bits& data,
                       const std::uint8_t (&end)[16]) {
        out.raw(begin, 16);
        const std::size_t at = out.pos();
        out.rl(static_cast<std::uint32_t>(data.b.size()));
        out.raw(data.b);
        out.rs(crc16(out.b.data() + at, out.pos() - at));
        out.raw(end, 16);
    }

    std::string assemble() {
        const auto [days, ms] = julian_now();
        struct Locator { std::uint8_t nr; std::uint32_t address = 0, size = 0; };
        std::array<Locator, 6> loc{};
        for (std::uint8_t i = 0; i < 6; ++i) loc[i].nr = i;

        Out out;
        out.zeros(0x61);  // file header, written last

        // AuxHeader (section 5).
        loc[5].address = static_cast<std::uint32_t>(out.pos());
        out.rc(0xFF); out.rc(0x77); out.rc(0x01);
        out.rs(kDwgVersion); out.rs(kMaintVersion);
        out.rl(1);                 // number of saves
        out.rl(0xFFFFFFFFu);
        out.rs(1); out.rs(0);
        out.rl(0);
        out.rs(kDwgVersion); out.rs(kMaintVersion);
        out.rs(kDwgVersion); out.rs(kMaintVersion);
        for (std::uint16_t v : {5, 0x893, 5, 0x893, 0, 1}) out.rs(v);
        out.zeros(20);
        out.rl(days); out.rl(ms);  // TDCREATE
        out.rl(days); out.rl(ms);  // TDUPDATE
        out.rl(static_cast<std::uint32_t>(handseed_));
        out.rl(0);                 // plot stamp
        out.rs(0); out.rs(1);
        out.rl(0); out.rl(0); out.rl(0);
        out.rl(1);
        out.rl(0); out.rl(0);
        out.zeros(8);
        loc[5].size = static_cast<std::uint32_t>(out.pos() - loc[5].address);

        // Preview: one empty header record.
        const std::uint32_t preview = static_cast<std::uint32_t>(out.pos());
        out.raw(kSentinelPreviewBegin, 16);
        out.rl(1 + 9 + 80);
        out.rc(1);                 // one record
        out.rc(1);                 // header data
        out.rl(static_cast<std::uint32_t>(out.pos() + 8));
        out.rl(80);
        out.zeros(80);
        out.raw(kSentinelPreviewEnd, 16);

        loc[0].address = static_cast<std::uint32_t>(out.pos());
        framed(out, kSentinelHeaderBegin, header_variables(days, ms), kSentinelHeaderEnd);
        loc[0].size = static_cast<std::uint32_t>(out.pos() - loc[0].address);

        loc[1].address = static_cast<std::uint32_t>(out.pos());
        framed(out, kSentinelClassesBegin, classes(), kSentinelClassesEnd);
        loc[1].size = static_cast<std::uint32_t>(out.pos() - loc[1].address);

        // Objects, in handle order.
        const std::uint32_t objects_at = static_cast<std::uint32_t>(out.pos());
        std::vector<std::pair<std::uint64_t, std::uint32_t>> map;
        map.reserve(objs_.size());
        for (auto& [handle, bytes] : objs_) {
            map.emplace_back(handle, static_cast<std::uint32_t>(out.pos()));
            out.raw(bytes);
            Bytes().swap(bytes);
        }

        // Object map: pages of <= 2032 bytes (big-endian size incl. itself, entries,
        // big-endian CRC); deltas restart from 0 on every page; an empty page ends it.
        loc[2].address = static_cast<std::uint32_t>(out.pos());
        {
            std::size_t i = 0;
            while (i < map.size()) {
                const std::size_t page = out.pos();
                out.rs_be(0);
                std::uint64_t last_handle = 0;
                std::int64_t last_loc = 0;
                // An entry is at most 15 bytes (UMC + signed MC); a page stays <= 2032
                // bytes including its size field and CRC.
                while (i < map.size() && out.pos() - page + 15 + 2 <= 2032) {
                    out.umc(map[i].first - last_handle);
                    out.mc(static_cast<std::int64_t>(map[i].second) - last_loc);
                    last_handle = map[i].first;
                    last_loc = map[i].second;
                    ++i;
                }
                const auto size = static_cast<std::uint16_t>(out.pos() - page);
                out.b[page] = static_cast<std::uint8_t>(size >> 8);
                out.b[page + 1] = static_cast<std::uint8_t>(size & 0xFF);
                out.rs_be(crc16(out.b.data() + page, size));
            }
            const std::size_t page = out.pos();
            out.rs_be(2);
            out.rs_be(crc16(out.b.data() + page, 2));
        }
        loc[2].size = static_cast<std::uint32_t>(out.pos() - loc[2].address);

        // ObjFreeSpace (section 3).
        loc[3].address = static_cast<std::uint32_t>(out.pos());
        out.rl(0);
        out.rl(static_cast<std::uint32_t>(map.size()));
        out.rl(days); out.rl(ms);
        out.rl(objects_at);
        out.rc(4);
        out.rll(0x32); out.rll(0x64); out.rll(0x200); out.rll(0xFFFFFFFFu);
        loc[3].size = static_cast<std::uint32_t>(out.pos() - loc[3].address);

        // Second header: a bit-coded copy of the file header locators and the key handles.
        // It records the template section that follows it, whose address depends on the
        // second header's own length (see the fixed-point loop below).
        const std::size_t second_at = out.pos();
        auto second_header = [&](std::uint32_t template_at) {
            Bits b;
            b.BL(static_cast<std::uint32_t>(second_at));
            const char version[11] = {'A', 'C', '1', '0', '1', '5', 0, 0, 0, 0, 0};
            for (char c : version) b.RC(static_cast<std::uint8_t>(c));
            b.RC(kMaintRelease);
            b.RC(1);
            b.BS((kMaintVersion << 8) | kDwgVersion);
            b.RS(kCodepage1252);
            b.BS(6);
            std::array<Locator, 6> l2 = loc;
            l2[4].address = template_at;
            l2[4].size = 4;
            for (const Locator& s : l2) {
                b.RC(s.nr);
                b.BL(s.address);
                b.BL(s.size);
            }
            const std::uint64_t handles[14] = {handseed_, hBlockCtl, hLayerCtl, hStyleCtl, hLtypeCtl,
                                               hViewCtl, hUcsCtl, hVportCtl, hAppidCtl, hDimstyleCtl,
                                               hVxCtl, hNod, hMlineDict, hGroupDict};
            b.BS(14);
            for (int k = 0; k < 14; ++k) {
                std::uint8_t len = 0;
                for (std::uint64_t t = handles[k]; t; t >>= 8) ++len;
                b.RC(len);
                b.RC(static_cast<std::uint8_t>(k));
                for (int j = len - 1; j >= 0; --j) b.RC(static_cast<std::uint8_t>(handles[k] >> (8 * j)));
            }
            return b;
        };
        // Size the second header with a provisional template address, then rebuild it with
        // the real one: a BL's width depends on its value, so iterate to a fixed point.
        std::uint32_t template_at = 0;
        Bits sh;
        for (int pass = 0; pass < 4; ++pass) {
            sh = second_header(template_at);
            const std::uint32_t next =
                static_cast<std::uint32_t>(second_at + 16 + 4 + sh.b.size() + 2 + 8 + 16);
            if (next == template_at) break;
            template_at = next;
        }
        out.raw(kSentinel2ndHeaderBegin, 16);
        const std::size_t size_at = out.pos();
        out.rl(static_cast<std::uint32_t>(4 + sh.b.size() + 2 + 8));  // size field .. junk
        out.raw(sh.b);
        out.rs(crc16(out.b.data() + size_at, out.pos() - size_at));
        out.zeros(8);
        out.raw(kSentinel2ndHeaderEnd, 16);
        if (out.pos() != template_at)
            throw std::runtime_error("write_dwg: internal error laying out the second header");

        // Template (section 4): empty description, MEASUREMENT.
        loc[4].address = static_cast<std::uint32_t>(out.pos());
        out.rs(0);
        out.rs(imperial_ ? 0 : 1);
        loc[4].size = static_cast<std::uint32_t>(out.pos() - loc[4].address);

        // File header.
        Out fh;
        const char magic[6] = {'A', 'C', '1', '0', '1', '5'};
        fh.raw(reinterpret_cast<const std::uint8_t*>(magic), 6);
        fh.zeros(5);
        fh.rc(kMaintRelease);
        fh.rc(1);
        fh.rl(preview);
        fh.rc(kDwgVersion);
        fh.rc(kMaintVersion);
        fh.rs(kCodepage1252);
        fh.rl(6);
        for (const Locator& s : loc) {
            fh.rc(s.nr);
            fh.rl(s.address);
            fh.rl(s.size);
        }
        fh.rs(crc16(fh.b.data(), fh.pos()));
        fh.raw(kSentinelFileHeaderEnd, 16);
        if (fh.pos() != 0x61) throw std::runtime_error("write_dwg: internal error in the file header size");
        std::copy(fh.b.begin(), fh.b.end(), out.b.begin());
        return std::string(out.b.begin(), out.b.end());
    }
};

}  // namespace

DwgImage build_dwg(const DxfDocument& doc) { return Writer(doc).build(); }

}  // namespace chudware::doc
