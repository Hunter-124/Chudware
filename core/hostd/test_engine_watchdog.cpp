// Unit test for the engine-call watchdog policy (engine_watchdog.h). Pure, no deps — compile &
// run standalone: g++ -std=c++17 core/hostd/test_engine_watchdog.cpp -o /tmp/tw && /tmp/tw
#include "engine_watchdog.h"

#include <cassert>
#include <cstdio>
#include <initializer_list>

int main() {
    using namespace hostd;

    const long long shortCeiling = 90000;
    const long long longCeiling = 3600000;  // 60 min — unchanged daemon default
    const auto overdue = [&](const char* method, long long elapsed) {
        return engineCallOverdue(method, elapsed, shortCeiling) ||
               engineCallOverdueLong(method, elapsed, longCeiling);
    };

    // Rebuild cost scales with input size, including clone preflight and cold
    // activation. Surviving the short deadline must not make these unbounded.
    for (const char* method : {"doc_apply_ops", "doc_rebuild", "doc_undo", "doc_redo",
                               "doc_activate", "doc_regen", "run_fea_static",
                               "run_cfd_flow", "agent_run", "cam_generate",
                               "doc_save", "doc_open", "export_part", "import_part"}) {
        assert(!overdue(method, shortCeiling + 1));
        assert(!overdue(method, 600000));  // ten-minute valid long operation
        assert(!overdue(method, longCeiling));
        assert(overdue(method, longCeiling + 1));
        assert(!engineCallOverdue(method, longCeiling + 1, shortCeiling));
        assert(!engineCallOverdueLong(method, longCeiling + 1, 0));
        assert(!engineCallOverdueLong(method, longCeiling + 1, -1));
    }

    // Truly bounded calls retain the short deadline, not the generous ceiling.
    for (const char* method : {"ping", "__hostd_test_stall"}) {
        assert(!overdue(method, 5000));
        assert(!overdue(method, shortCeiling));
        assert(overdue(method, shortCeiling + 1));
        assert(!engineCallOverdueLong(method, longCeiling + 1, longCeiling));
        assert(!engineCallOverdue(method, longCeiling + 1, 0));
        assert(!engineCallOverdue(method, longCeiling + 1, -1));
    }

    std::puts("engine_watchdog: all assertions passed");
    return 0;
}
