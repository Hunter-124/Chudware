// Unit test for the engine-call watchdog policy (engine_watchdog.h). Pure, no deps — compile &
// run standalone: g++ -std=c++17 core/hostd/test_engine_watchdog.cpp -o /tmp/tw && /tmp/tw
#include "engine_watchdog.h"

#include <cassert>
#include <cstdio>

int main() {
    using namespace hostd;

    // A watchdogged interactive RPC past the ceiling is overdue; under it is not.
    assert(engineCallOverdue("doc_apply_ops", 200000, 180000));
    assert(!engineCallOverdue("doc_apply_ops", 5000, 180000));
    assert(!engineCallOverdue("doc_apply_ops", 180000, 180000));  // exactly at ceiling: not yet

    // Every guarded interactive method is classified.
    for (const char* m : {"doc_apply_ops", "doc_rebuild", "doc_undo", "doc_redo",
                          "doc_activate", "doc_regen"})
        assert(engineMethodWatchdogged(m));

    // Legitimately long-running RPCs are EXEMPT — never overdue no matter how long they run.
    for (const char* m : {"run_fea_static", "run_cfd_flow", "agent_run", "cam_generate",
                          "doc_save", "doc_open", "export_part", "import_part"}) {
        assert(!engineMethodWatchdogged(m));
        assert(!engineCallOverdue(m, 3600000, 180000));
    }

    // Daemon-only methods (never engine calls) are not guarded.
    assert(!engineMethodWatchdogged("hello"));
    assert(!engineMethodWatchdogged("daemon_status"));

    // ceilingMs <= 0 disables the watchdog entirely.
    assert(!engineCallOverdue("doc_apply_ops", 999999999, 0));
    assert(!engineCallOverdue("doc_apply_ops", 999999999, -1));

    std::puts("engine_watchdog: all assertions passed");
    return 0;
}
