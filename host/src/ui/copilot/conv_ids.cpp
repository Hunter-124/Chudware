// Session-id allocation — see conv_ids.h for why this is its own GL-free translation unit.
#include "conv_ids.h"

#include <algorithm>
#include <cctype>

namespace ui {

int convIdOrdinal(const std::string& id) {
    if (id.size() < 2 || id[0] != 'c') return 0;
    for (size_t i = 1; i < id.size(); ++i)
        if (!std::isdigit((unsigned char)id[i])) return 0;
    // Long digit runs are junk, not an ordinal — refuse rather than throw or wrap.
    if (id.size() > 10) return 0;
    long n = 0;
    for (size_t i = 1; i < id.size(); ++i) n = n * 10 + (id[i] - '0');
    return (n > 0 && n < 1000000000L) ? (int)n : 0;
}

std::string nextFreeConvId(int& counter,
                           const std::vector<std::string>& taken,
                           bool (*existsOnDisk)(const std::string&, void*),
                           void* ctx) {
    for (;;) {
        const std::string id = "c" + std::to_string(++counter);
        if (std::find(taken.begin(), taken.end(), id) != taken.end()) continue;
        if (existsOnDisk && existsOnDisk(id, ctx)) continue;
        return id;
    }
}

}  // namespace ui
