// Shared normalized ACIS tokens and SAT parser; no geometry construction dependencies.
// Included inside chudware::doc after standard array/charconv/map/string/vector headers.
// ---------------------------------------------------------------------------------------------
// Normalised token stream shared by SAT and SAB.

enum class TK : std::uint8_t { Num, Ptr, Str, Word, Bool, Enum, Vec, Vec2, Open, Close };

struct Tok {
    TK k = TK::Num;
    bool integral = false;                    // Num: written as an integer (SAT) / int tag (SAB)
    bool b = false;                           // Bool (SAB 0x0A / 0x0B)
    std::int64_t i = 0;                       // integral Num / Ptr / Enum
    double d = 0.0;                           // Num
    std::array<double, 3> v{0.0, 0.0, 0.0};   // Vec / Vec2
    std::string s;                            // Str / Word
};

struct Rec {
    std::string type;   // full ACIS type, e.g. "cone-surface", "tvertex-vertex"
    std::string base;   // last '-' component: "surface", "vertex", "attrib", ...
    std::string kind;   // first component: "cone", "tvertex", "body", ...
    std::vector<Tok> t;
};

struct Model {
    int version = 0;
    bool binary = false;
    double units = 0.0;
    double res_tol = 1e-6;
    std::vector<Rec> recs;
    // Subtype blocks in file order (the `ref N` index) -> (record, index of the Open token).
    std::vector<std::pair<int, std::size_t>> subtypes;
    std::map<std::pair<int, std::size_t>, int> subtype_index;
};

[[noreturn]] void fail(const std::string& what) { throw std::runtime_error("ACIS: " + what); }

bool is_end_marker(std::string_view w) {
    return w.rfind("End-of-ACIS-data", 0) == 0 || w.rfind("End-of-ASM-data", 0) == 0 ||
           w.rfind("Begin-of-ACIS-History-data", 0) == 0;
}

void split_type(Rec& r) {
    const auto last = r.type.rfind('-');
    r.base = last == std::string::npos ? r.type : r.type.substr(last + 1);
    const auto first = r.type.find('-');
    r.kind = first == std::string::npos ? r.type : r.type.substr(0, first);
}

bool parse_number(std::string_view w, Tok& out) {
    if (w.empty()) return false;
    const char c0 = w[0];
    if (!(std::isdigit(static_cast<unsigned char>(c0)) || c0 == '-' || c0 == '+' || c0 == '.'))
        return false;
    if (c0 == '+') w.remove_prefix(1);
    if (w.empty()) return false;
    const char* b = w.data();
    const char* e = b + w.size();
    double d = 0.0;
    auto r = std::from_chars(b, e, d);
    if (r.ec != std::errc() || r.ptr != e) return false;
    out.k = TK::Num;
    out.d = d;
    std::int64_t iv = 0;
    auto ri = std::from_chars(b, e, iv);
    out.integral = ri.ec == std::errc() && ri.ptr == e;
    out.i = out.integral ? iv : 0;
    return true;
}

void index_subtypes(Model& m) {
    for (int r = 0; r < static_cast<int>(m.recs.size()); ++r) {
        const auto& t = m.recs[r].t;
        for (std::size_t i = 0; i < t.size(); ++i) {
            if (t[i].k != TK::Open) continue;
            const bool is_ref = i + 1 < t.size() &&
                                (t[i + 1].k == TK::Word || t[i + 1].k == TK::Str) &&
                                t[i + 1].s == "ref";
            if (is_ref) continue;
            m.subtype_index[{r, i}] = static_cast<int>(m.subtypes.size());
            m.subtypes.emplace_back(r, i);
        }
    }
}

// ---------------------------------------------------------------------------------------------
// SAT text.

Model parse_sat(std::string_view s) {
    Model m;
    std::size_t p = 0;
    const std::size_t n = s.size();
    auto next_line = [&]() -> std::string_view {
        while (p < n) {
            std::size_t e = s.find('\n', p);
            if (e == std::string_view::npos) e = n;
            std::string_view line = s.substr(p, e - p);
            p = e < n ? e + 1 : n;
            if (line.find_first_not_of(" \t\r") != std::string_view::npos) return line;
        }
        return {};
    };
    auto words = [](std::string_view line) {
        std::vector<std::string_view> w;
        std::size_t i = 0;
        while (i < line.size()) {
            while (i < line.size() && std::isspace(static_cast<unsigned char>(line[i]))) ++i;
            std::size_t b = i;
            while (i < line.size() && !std::isspace(static_cast<unsigned char>(line[i]))) ++i;
            if (i > b) w.push_back(line.substr(b, i - b));
        }
        return w;
    };

    // Line 1: "<version> <records> <bodies> <flags>".
    {
        auto w = words(next_line());
        Tok t;
        if (w.empty() || !parse_number(w[0], t) || !t.integral)
            fail("not SAT data (first line has no version number)");
        m.version = static_cast<int>(t.i);
    }
    // Line 2: product / ACIS version / date as counted strings ("@N text" or "N text");
    // line 3: "<mm per unit> <resabs> <resnor>". Both are absent in the oldest files.
    auto all_numbers = [](const std::vector<std::string_view>& w) {
        Tok t;
        for (auto x : w)
            if (!parse_number(x, t)) return false;
        return !w.empty();
    };
    std::size_t save = p;
    auto l2 = words(next_line());
    if (l2.size() == 3 && all_numbers(l2)) {
        p = save;   // no product line; this is the units line
    } else if (!l2.empty()) {
        Tok t;
        std::string_view w0 = l2[0];
        if (!w0.empty() && w0[0] == '@') w0.remove_prefix(1);
        if (!parse_number(w0, t)) p = save;   // records start right away
    }
    save = p;
    auto l3 = words(next_line());
    if (l3.size() >= 2 && all_numbers(l3)) {
        Tok t;
        parse_number(l3[0], t);
        m.units = t.d;
        parse_number(l3[1], t);
        if (t.d > 0.0) m.res_tol = t.d;
    } else {
        p = save;
    }

    // Records: whitespace-separated tokens, '#' terminates a record, "@N " introduces an
    // N-character string that may contain spaces or '#'.
    Rec cur;
    bool in_rec = false;
    bool finished = false;
    while (p < n && !finished) {
        while (p < n && std::isspace(static_cast<unsigned char>(s[p]))) ++p;
        if (p >= n) break;
        if (in_rec && s[p] == '@' && p + 1 < n && std::isdigit(static_cast<unsigned char>(s[p + 1]))) {
            std::size_t q = p + 1;
            std::size_t count = 0;
            while (q < n && std::isdigit(static_cast<unsigned char>(s[q]))) {
                const auto digit = static_cast<std::size_t>(s[q] - '0');
                if (count > (std::numeric_limits<std::size_t>::max() - digit) / 10)
                    fail("SAT counted string length overflows");
                count = count * 10 + digit;
                ++q;
            }
            if (q >= n || s[q] != ' ') fail("SAT counted string lacks its separator");
            ++q;
            if (count > n - q) fail("SAT counted string exceeds remaining input");
            Tok t;
            t.k = TK::Str;
            t.s = std::string(s.substr(q, count));
            cur.t.push_back(std::move(t));
            p = q + count;  // q advanced past the prefix; the validated sum cannot wrap
            continue;
        }
        std::size_t b = p;
        while (p < n && !std::isspace(static_cast<unsigned char>(s[p]))) ++p;
        std::string_view w = s.substr(b, p - b);
        bool ends = false;
        if (w == "#") {
            ends = true;
            w = {};
        } else if (w.size() > 1 && w.back() == '#') {
            w.remove_suffix(1);
            ends = true;
        }
        if (!w.empty()) {
            if (!in_rec) {
                if (is_end_marker(w)) {
                    finished = true;
                    break;
                }
                Tok seq;
                if (w[0] == '-' && parse_number(w, seq) && seq.integral) continue;   // "-12 face"
                cur = Rec{};
                cur.type = std::string(w);
                in_rec = true;
            } else {
                Tok t;
                if (w[0] == '$' && w.size() > 1 && parse_number(w.substr(1), t) && t.integral) {
                    t.k = TK::Ptr;
                } else if (w == "{") {
                    t = Tok{};
                    t.k = TK::Open;
                } else if (w == "}") {
                    t = Tok{};
                    t.k = TK::Close;
                } else if (!parse_number(w, t)) {
                    t = Tok{};
                    t.k = TK::Word;
                    t.s = std::string(w);
                }
                cur.t.push_back(std::move(t));
            }
        }
        if (ends && in_rec) {
            split_type(cur);
            m.recs.push_back(std::move(cur));
            cur = Rec{};
            in_rec = false;
        }
    }
    if (in_rec)
        fail("SAT record " + std::to_string(m.recs.size()) + " (" + cur.type +
             ") is not terminated by '#'");
    index_subtypes(m);
    return m;
}
