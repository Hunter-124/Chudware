// tool_phrase — see tool_phrase.h for why this is its own GL-free translation unit.
#include "tool_phrase.h"

#include <cctype>

namespace ui {

// ===========================================================================================
// 1. the phrasing table
// ===========================================================================================

namespace {

struct Entry {
    const char* tool;
    const char* title;
    bool        mutating;
};

// Every tool observed in a real run, plus the obvious families around each one. Present
// participles throughout: the card says what is happening right now, and the same string reads
// correctly once the call has finished ("Building geometry × 3").
//
// `mutating` is the honest question "did this change the model or the disk?", not "did this cost
// anything" — a render is expensive and still leaves nothing behind.
//
// Linear scan: ~45 entries of short strings, and the caller already does string work per row.
constexpr Entry kTable[] = {
    // -- geometry and document lifecycle (mutating) --
    {"add_features",             "Building geometry",           true},
    {"new_part",                 "Creating a part",             true},
    {"new_assembly",             "Creating an assembly",        true},
    {"import_part",              "Importing a part",            true},
    {"save_part",                "Saving the part",             true},
    {"export_part",              "Exporting the part",          true},
    {"close_part",               "Closing the part",            true},

    // -- looking at the model (read-only) --
    {"render_part",              "Rendering the part",          false},
    {"render_views",             "Rendering views",             false},
    {"contact_sheet",            "Building a contact sheet",    false},
    {"doc_sketch_contact_sheet", "Rendering sketches",          false},
    {"inspect_part",             "Inspecting the part",         false},
    {"inspect_feature",          "Inspecting a feature",        false},
    {"look_around",              "Looking around the part",     false},
    {"describe_part",            "Reading the part",            false},
    {"get_part",                 "Reading the part",            false},
    {"list_parts",               "Listing open parts",          false},
    {"measure",                  "Measuring",                   false},
    {"measure_between",          "Measuring",                   false},
    {"measure_part",             "Measuring",                   false},

    // -- checks and reviews (read-only) --
    {"check_interference",       "Checking interference",       false},
    {"verify_battery",           "Running verification",        false},
    {"design_review",            "Reviewing the design",        false},
    {"check_sketch",             "Checking the sketch",         false},
    {"interface_check",          "Checking interfaces",         false},

    // -- the project folder --
    {"fs_list",                  "Reading the project folder",  false},
    {"fs_glob",                  "Searching the project folder", false},
    {"fs_read",                  "Reading a file",              false},
    {"fs_write",                 "Writing a file",              true},
    {"fs_edit",                  "Editing a file",              true},

    // -- harness surfaces --
    {"cad_guide",                "Consulting the modeling guide", false},
    {"enable_tools",             "Enabling tools",              false},
    {"ledger_note",              "Recording a decision",        true},
    {"ask_user",                 "Asking you a question",       false},
    {"delegate",                 "Delegating to a specialist",  true},

    // -- history and drawings --
    {"undo",                     "Undoing",                     true},
    {"redo",                     "Redoing",                     true},
    {"plm_rollback",             "Rolling back",                true},
    {"make_drawing",             "Creating a drawing",          true},
    {"export_drawing",           "Exporting the drawing",       true},
    {"add_datum",                "Adding a datum",              true},
    {"add_dimension",            "Adding a dimension",          true},
};

const Entry* lookup(std::string_view tool) {
    for (const Entry& e : kTable) {
        if (tool == e.tool) return &e;
    }
    return nullptr;
}

// `foo_bar_baz` -> "Foo bar baz". Never leave a bare MCP symbol on screen: an unknown tool is
// still a sentence fragment a person can read, and it makes the missing table entry obvious to
// whoever added the tool.
std::string fallbackTitle(std::string_view tool) {
    std::string out(tool);
    for (char& c : out) {
        if (c == '_') c = ' ';
    }
    for (char& c : out) {
        if (std::isalpha((unsigned char)c) != 0) {
            c = (char)std::toupper((unsigned char)c);
            break;
        }
        if (c != ' ') break;  // a leading digit or symbol: nothing to capitalise
    }
    return out;
}

// Read-only inspection tools fold across their whole FAMILY, because three back-to-back renders
// of the same part are one thing that happened, not three. Everything else folds only with the
// identical tool.
std::string_view groupFamily(std::string_view tool) {
    if (tool == "render_part" || tool == "render_views" || tool == "contact_sheet" ||
        tool == "doc_sketch_contact_sheet") {
        return "render";
    }
    if (tool == "measure" || tool == "measure_between" || tool == "measure_part") {
        return "measure";
    }
    return {};
}

// Calls that must always stay individually visible even when they succeed. new_part /
// new_assembly / import_part each bring a DOCUMENT into existence — folding two of those is how a
// duplicate part hides — and ask_user / delegate are addressed at a person.
bool neverGroups(std::string_view tool) {
    return tool == "ask_user" || tool == "delegate" || tool == "new_part" ||
           tool == "new_assembly" || tool == "import_part";
}

}  // namespace

std::string toolGroupTitle(std::string_view tool, int count) {
    const Entry* e  = lookup(tool);
    std::string title = e ? std::string(e->title) : fallbackTitle(tool);
    if (count <= 1) return title;  // "Rendering the part × 1" is noise
    title += " \xc3\x97 ";         // U+00D7 MULTIPLICATION SIGN
    title += std::to_string(count);
    return title;
}

ToolPhrase toolPhrase(std::string_view tool, std::string_view target, bool ok) {
    const Entry* e = lookup(tool);

    ToolPhrase p;
    p.title    = e ? std::string(e->title) : fallbackTitle(tool);
    p.mutating = e ? e->mutating : false;
    p.detail   = humanTarget(target);

    if (ok && !neverGroups(tool) && !tool.empty()) {
        std::string_view family = groupFamily(tool);
        p.groupKey = family.empty() ? std::string(tool) : std::string(family);
    }
    return p;
}

// ===========================================================================================
// 2. humanTarget
// ===========================================================================================

namespace {

inline bool isSpace(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

std::string collapseWhitespace(std::string_view s) {
    std::string out;
    out.reserve(s.size());
    bool pending = false;  // a run of whitespace is owed, but only if real text follows
    for (char c : s) {
        if (isSpace(c)) {
            pending = !out.empty();
            continue;
        }
        if (pending) {
            out.push_back(' ');
            pending = false;
        }
        out.push_back(c);
    }
    return out;
}

void appendUtf8(std::string& out, unsigned cp) {
    if (cp < 0x80) {
        out.push_back((char)cp);
    } else if (cp < 0x800) {
        out.push_back((char)(0xC0 | (cp >> 6)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else if (cp < 0x10000) {
        out.push_back((char)(0xE0 | (cp >> 12)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    } else {
        out.push_back((char)(0xF0 | (cp >> 18)));
        out.push_back((char)(0x80 | ((cp >> 12) & 0x3F)));
        out.push_back((char)(0x80 | ((cp >> 6) & 0x3F)));
        out.push_back((char)(0x80 | (cp & 0x3F)));
    }
}

int hexVal(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

// Read four hex digits at *i* (which sits just past the 'u'); -1 when they are not all hex.
int hex4(std::string_view s, std::size_t i) {
    if (i + 4 > s.size()) return -1;
    int v = 0;
    for (std::size_t k = 0; k < 4; ++k) {
        int d = hexVal(s[i + k]);
        if (d < 0) return -1;
        v = (v << 4) | d;
    }
    return v;
}

// Parse the JSON string starting at s[i] == '"'. On success *out* receives the decoded value and
// *i* lands just past the closing quote. On any malformation this returns false and *i* is
// unspecified — the caller then leaves the whole target alone.
bool parseJsonString(std::string_view s, std::size_t& i, std::string& out) {
    if (i >= s.size() || s[i] != '"') return false;
    ++i;
    out.clear();
    while (i < s.size()) {
        char c = s[i];
        if (c == '"') {
            ++i;
            return true;
        }
        if (c != '\\') {
            out.push_back(c);
            ++i;
            continue;
        }
        ++i;
        if (i >= s.size()) return false;
        char esc = s[i++];
        switch (esc) {
            case '"':  out.push_back('"');  break;
            case '\\': out.push_back('\\'); break;
            case '/':  out.push_back('/');  break;
            case 'b':  out.push_back('\b'); break;
            case 'f':  out.push_back('\f'); break;
            case 'n':  out.push_back('\n'); break;
            case 'r':  out.push_back('\r'); break;
            case 't':  out.push_back('\t'); break;
            case 'u': {
                int hi = hex4(s, i);
                if (hi < 0) return false;
                i += 4;
                unsigned cp = (unsigned)hi;
                if (cp >= 0xD800 && cp <= 0xDBFF && i + 6 <= s.size() && s[i] == '\\' &&
                    s[i + 1] == 'u') {
                    int lo = hex4(s, i + 2);
                    if (lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + ((unsigned)lo - 0xDC00);
                        i += 6;
                    }
                }
                if (cp >= 0xD800 && cp <= 0xDFFF) return false;  // lone surrogate
                appendUtf8(out, cp);
                break;
            }
            default: return false;
        }
    }
    return false;  // unterminated
}

std::string trimmed(std::string_view s) {
    std::size_t a = 0, b = s.size();
    while (a < b && isSpace(s[a])) ++a;
    while (b > a && isSpace(s[b - 1])) --b;
    return std::string(s.substr(a, b - a));
}

// `{"doc_id": "mudbucket"}` -> mudbucket. Exactly one key, or nothing happens: a two-key object
// is an arguments blob, and picking one of its values would silently drop the rest.
bool unwrapSingleKeyObject(const std::string& s, std::string& out) {
    if (s.size() < 2 || s.front() != '{' || s.back() != '}') return false;

    std::size_t i = 1;
    while (i < s.size() && isSpace(s[i])) ++i;
    std::string key;
    if (!parseJsonString(s, i, key)) return false;  // key must be a JSON string
    while (i < s.size() && isSpace(s[i])) ++i;
    if (i >= s.size() || s[i] != ':') return false;
    ++i;

    // The value runs to the closing brace. A depth-0 comma means a second key.
    std::size_t valueStart = i;
    int depth = 0;
    while (i < s.size()) {
        char c = s[i];
        if (c == '"') {
            std::string ignored;
            if (!parseJsonString(s, i, ignored)) return false;
            continue;
        }
        if (c == '{' || c == '[') {
            ++depth;
        } else if (c == ']') {
            --depth;
        } else if (c == '}') {
            if (depth == 0) break;
            --depth;
        } else if (c == ',' && depth == 0) {
            return false;  // more than one key
        }
        ++i;
    }
    if (i != s.size() - 1) return false;  // the brace we stopped at must be the final one

    std::string value = trimmed(std::string_view(s).substr(valueStart, i - valueStart));
    if (value.empty()) return false;

    // A string value is unwrapped to its text; anything else (number, nested object) is shown
    // verbatim — it is still shorter and more honest than the whole envelope.
    if (value.front() == '"') {
        std::size_t j = 0;
        std::string decoded;
        if (parseJsonString(value, j, decoded) && j == value.size()) {
            out = decoded;
            return true;
        }
        return false;
    }
    out = value;
    return true;
}

// Truncate to *maxChars* codepoints, appending an ellipsis. Continuation bytes (10xxxxxx) are
// never counted and never left dangling, so the result is always valid UTF-8.
std::string ellipsize(const std::string& s, std::size_t maxChars) {
    std::size_t chars = 0;
    std::size_t cut   = std::string::npos;
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (((unsigned char)s[i] & 0xC0) == 0x80) continue;  // continuation byte
        if (chars == maxChars) {
            cut = i;
            break;
        }
        ++chars;
    }
    if (cut == std::string::npos) return s;  // fits
    return s.substr(0, cut) + "\xe2\x80\xa6";  // U+2026 HORIZONTAL ELLIPSIS
}

}  // namespace

std::string humanTarget(std::string_view target, std::size_t maxChars) {
    std::string s = collapseWhitespace(target);
    if (s.empty()) return s;

    // A lone JSON string: `"foo"` -> foo.
    if (s.front() == '"') {
        std::size_t i = 0;
        std::string decoded;
        if (parseJsonString(s, i, decoded) && i == s.size()) {
            s = collapseWhitespace(decoded);
        }
    } else {
        std::string value;
        if (unwrapSingleKeyObject(s, value)) {
            s = collapseWhitespace(value);
        }
    }

    return ellipsize(s, maxChars);
}

}  // namespace ui
