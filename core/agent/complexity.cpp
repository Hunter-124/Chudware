// core/agent/complexity.cpp — see complexity.h.
#include "agent/complexity.h"

#include <algorithm>
#include <array>
#include <cctype>

namespace aicad {
namespace agent {

namespace {

std::string lower(const std::string& s) {
    std::string o = s;
    std::transform(o.begin(), o.end(), o.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return o;
}

int count_occurrences(const std::string& hay, const std::string& needle) {
    if (needle.empty()) return 0;
    int n = 0;
    for (size_t p = hay.find(needle); p != std::string::npos; p = hay.find(needle, p + needle.size()))
        ++n;
    return n;
}

// whole-word-ish containment (needle bounded by non-alnum or string edge) so
// "or" doesn't match "motor" and "nut" doesn't match "nutcracker".
bool has_word(const std::string& hay, const std::string& w) {
    size_t p = hay.find(w);
    while (p != std::string::npos) {
        const bool lok = (p == 0) || !std::isalnum((unsigned char)hay[p - 1]);
        const size_t e = p + w.size();
        const bool rok = (e >= hay.size()) || !std::isalnum((unsigned char)hay[e]);
        if (lok && rok) return true;
        p = hay.find(w, p + 1);
    }
    return false;
}

// A conjunction (" and ", " plus ", ";") joins OBJECTS unless it merely chains a DIMENSION
// onto the previous phrase — e.g. "20mm in diameter and 30mm tall": the ' and ' is followed by
// a number+unit, not a new object. `dimensional_after` returns true when the text right after a
// conjunction is <number><length/angle-unit>, so a terse single primitive with two dimensions
// still routes SOLO instead of being mistaken for two joined objects.
bool dimensional_after(const std::string& r, size_t after) {
    while (after < r.size() && r[after] == ' ') ++after;
    size_t p = after;
    if (p >= r.size() || !std::isdigit((unsigned char)r[p])) return false;  // must start with a number
    while (p < r.size() && (std::isdigit((unsigned char)r[p]) || r[p] == '.')) ++p;
    while (p < r.size() && r[p] == ' ') ++p;  // optional space between number and unit
    static const std::array<const char*, 9> kUnits = {"mm", "cm", "in", "inch", "\"", "deg",
                                                      "mil", "thou", "m"};
    for (const char* u : kUnits) {
        const size_t ul = std::string(u).size();
        if (r.compare(p, ul, u) == 0) {
            // whole-token: the unit must not run into a longer word ("mm"/"in" ok, "mint" not).
            const size_t e = p + ul;
            if (e >= r.size() || !std::isalpha((unsigned char)r[e])) return true;
        }
    }
    return false;
}

// Count only conjunctions that join OBJECTS (dimensional continuations excluded).
int count_object_conjunctions(const std::string& r) {
    int n = count_occurrences(r, ";");  // semicolons always separate clauses/objects
    for (const char* c : {" and ", " plus "}) {
        const std::string s(c);
        for (size_t p = r.find(s); p != std::string::npos; p = r.find(s, p + s.size()))
            if (!dimensional_after(r, p + s.size())) ++n;
    }
    return n;
}

int word_count(const std::string& s) {
    int n = 0;
    bool in = false;
    for (char c : s) {
        if (std::isspace((unsigned char)c)) {
            in = false;
        } else if (!in) {
            in = true;
            ++n;
        }
    }
    return n;
}

}  // namespace

ComplexityVerdict classify_complexity(const std::string& request) {
    const std::string r = lower(request);
    const int words = word_count(r);

    // --- COMPLEX: assemblies, mechanisms, multi-body systems, whole devices ----
    // "system" / "machine" are word-bounded and exclude common false-positive
    // bigrams ("coordinate system", "state machine") that are not multi-part builds.
    // Corpus: "two brackets that bolt together with M5 assembly" → Complex
    static const std::array<const char*, 20> kComplex = {
        "assembly", "assemblies", "mechanism", "gearbox", "gear train", "linkage",
        "enclosure with", "housing with", "robot", "drone",
        "actuator", "transmission", "chassis", "subsystem", "multiple parts",
        "several parts", "mount and", "assemble", "mating", "moving parts"};
    for (const char* k : kComplex)
        if (r.find(k) != std::string::npos)
            return {Complexity::Complex, std::string("mentions '") + k + "' (assembly/mechanism)"};

    // Multi-part intent phrasing (two bodies that fasten / attach).
    // Corpus: "two brackets that bolt together with M5 assembly" → Complex (also via assembly)
    // Corpus: hinge multi-leaf language stays below unless these fire.
    static const std::array<const char*, 6> kMultiPart = {
        "that bolt together", "that screw together", "bolt together", "screw together",
        "mounts to", "attaches to"};
    for (const char* k : kMultiPart)
        if (r.find(k) != std::string::npos)
            return {Complexity::Complex, std::string("mentions '") + k + "' (multi-part intent)"};

    // Word-bounded "system" / "machine", skipping benign bigrams.
    // Corpus: "a coordinate system at the centroid" → NOT Complex (coordinate system excluded)
    if (has_word(r, "system") && r.find("coordinate system") == std::string::npos)
        return {Complexity::Complex, "mentions 'system' (assembly/mechanism)"};
    if (has_word(r, "machine") && r.find("state machine") == std::string::npos)
        return {Complexity::Complex, "mentions 'machine' (assembly/mechanism)"};

    // conjunction density: " A and B ", "A; B", "A plus B" join multiple OBJECTS. Bare commas are
    // NOT counted — dimension/attribute lists ("22mm tall, M3 bore") use non-joining commas. A
    // conjunction that only chains a DIMENSION ("...diameter and 30mm tall") is excluded too, so a
    // plain primitive with two dimensions still routes SOLO instead of over-planning.
    const int conjunctions = count_object_conjunctions(r);

    // --- STANDARD floor: multi-feature single bodies that the short-path would Solo ---
    // Corpus: "sheetmetal enclosure 200x120x60mm, 1.5mm steel" → Standard (formed part with
    // bends/flat pattern; words=5 would otherwise hit the short Solo path).
    // "enclosure with" / "housing with" already returned Complex above.
    if (has_word(r, "enclosure") || has_word(r, "housing"))
        return {Complexity::Standard, "formed enclosure/housing (multi-feature part)"};

    // Sheet-metal fab language implies sm_* ops (flange/bend/hem/flat), not a one-loop
    // primitive — even when the prompt is terse. Catalog fasteners naming only a material
    // thickness stay Solo via the noun path before any broader sheet cue is needed.
    // Corpus: "sheetmetal enclosure …" also covered above; plain "sheet metal bracket …"
    // with extra features lands here as Standard when not a bare solo noun short-form.
    const bool sheetmetal_cue = has_word(r, "sheetmetal") || has_word(r, "sheet metal") ||
                                r.find("sheet-metal") != std::string::npos;
    if (sheetmetal_cue && conjunctions == 0 && words > 4)
        return {Complexity::Standard, "sheet-metal multi-feature part"};

    // --- SOLO: a single recognizable primitive / catalog part, tersely stated ---
    // Corpus: standoff / washer / knob / chamfer edit / garbage → Solo
    // Surface patterns (knurling) must NOT inflate complexity — knurled/knurling are
    // not in any elevator list.
    static const std::array<const char*, 39> kSoloNouns = {
        "standoff", "spacer", "screw", "bolt", "nut", "washer", "shim", "bearing",
        "o-ring", "oring", "dowel", "pin", "bushing", "insert", "grommet", "plug",
        "cap", "bracket", "plate", "block", "cube", "cylinder", "disc", "disk",
        "rod", "tube", "ring", "hook", "knob", "peg", "flange",
        // single geometric primitives (one sketch+extrude/revolve — no plan needed)
        "prism", "cone", "sphere", "pyramid", "wedge", "torus", "hexagon", "polygon"};
    bool solo_noun = false;
    for (const char* n : kSoloNouns)
        if (has_word(r, n)) { solo_noun = true; break; }

    // A terse request naming a single simple noun, with no conjunctions joining
    // multiple objects, is a solo one-loop build.
    // Corpus: "a knurled hex standoff, 22mm tall, M3 clearance bore" → Solo
    // Corpus: "M6 washer, 1.5mm thick" → Solo
    // Corpus: "knob with knurling pattern" → Solo (pattern is surface finish, not multi-part)
    if (solo_noun && conjunctions == 0 && words <= 18)
        return {Complexity::Solo, "single simple part, no conjunctions"};

    // Very short requests with no conjunction are almost always one object.
    // Corpus: "chamfer the top edges (edit follow-up, existing doc)" → Solo
    // Corpus: "a coordinate system at the centroid" → Solo (not Complex; see bigram guard)
    // Corpus: "asdf qwerty blah blah" → Solo (rejection is intake's job, not classifier)
    if (conjunctions == 0 && words <= 8)
        return {Complexity::Solo, "very short single-object request"};

    // --- STANDARD: everything else (a normal multi-feature part) --------------
    // Corpus: "mounting plate with 4 clearance holes and 2 ribs" → Standard
    // Corpus: "hinge bracket with two leaves and a pin" → Standard (multi-feature;
    // Complex only if multi-part phrasing above fires)
    return {Complexity::Standard, "multi-feature part or joined objects"};
}

ExecStructure recommend_structure(Complexity level, bool cross_discipline,
                                  bool allow_company) {
    switch (level) {
        case Complexity::Solo:     return ExecStructure::Solo;
        case Complexity::Standard: return ExecStructure::Subagent;
        case Complexity::Complex:
            // Company only when explicitly allowed (Settings agent.corporate_mode).
            // Cross-discipline alone must not auto-invoke company.
            return (cross_discipline && allow_company) ? ExecStructure::Company
                                                      : ExecStructure::Dag;
    }
    return ExecStructure::Subagent;
}

const char* structure_label(ExecStructure s) {
    switch (s) {
        case ExecStructure::Solo:     return "solo";
        case ExecStructure::Subagent: return "subagent";
        case ExecStructure::Dag:      return "dag";
        case ExecStructure::Company:  return "company";
    }
    return "subagent";
}

}  // namespace agent
}  // namespace aicad
