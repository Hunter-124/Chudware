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
    static const std::array<const char*, 22> kComplex = {
        "assembly", "assemblies", "mechanism", "gearbox", "gear train", "linkage",
        "enclosure with", "housing with", "system", "machine", "robot", "drone",
        "actuator", "transmission", "chassis", "subsystem", "multiple parts",
        "several parts", "mount and", "assemble", "mating", "moving parts"};
    for (const char* k : kComplex)
        if (r.find(k) != std::string::npos)
            return {Complexity::Complex, std::string("mentions '") + k + "' (assembly/mechanism)"};

    // conjunction density: " A and B ", "A; B", "A plus B" join multiple OBJECTS.
    // Bare commas are NOT counted — dimension/attribute lists ("22mm tall, M3 bore")
    // use commas that don't join objects (the canonical standoff case).
    const int conjunctions = count_occurrences(r, " and ") + count_occurrences(r, " plus ") +
                             count_occurrences(r, ";");

    // --- SOLO: a single recognizable primitive / catalog part, tersely stated ---
    static const std::array<const char*, 30> kSoloNouns = {
        "standoff", "spacer", "screw", "bolt", "nut", "washer", "shim", "bearing",
        "o-ring", "oring", "dowel", "pin", "bushing", "insert", "grommet", "plug",
        "cap", "bracket", "plate", "block", "cube", "cylinder", "disc", "disk",
        "rod", "tube", "ring", "hook", "knob", "peg"};
    bool solo_noun = false;
    for (const char* n : kSoloNouns)
        if (has_word(r, n)) { solo_noun = true; break; }

    // A terse request naming a single simple noun, with no conjunctions joining
    // multiple objects, is a solo one-loop build.
    if (solo_noun && conjunctions == 0 && words <= 18)
        return {Complexity::Solo, "single simple part, no conjunctions"};

    // Very short requests with no conjunction are almost always one object.
    if (conjunctions == 0 && words <= 8)
        return {Complexity::Solo, "very short single-object request"};

    // --- STANDARD: everything else (a normal multi-feature part) --------------
    return {Complexity::Standard, "multi-feature part or joined objects"};
}

}  // namespace agent
}  // namespace aicad
