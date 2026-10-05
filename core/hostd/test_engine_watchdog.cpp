// Unit test for the engine-call watchdog policy (engine_watchdog.h): which hostd calls the
// short and long wall-clock ceilings may end, and that agent/chudcode turns are never capped.
// Registered as ctest `engine_watchdog`; doctest checks stay active in Release (NDEBUG).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "engine_watchdog.h"

#include <initializer_list>

using hostd::engineCallOverdue;
using hostd::engineCallOverdueLong;

namespace {

constexpr long long kShortCeiling = 90000;
constexpr long long kLongCeiling = 3600000;  // 60 min — daemon default

bool overdue(const char* method, long long elapsed) {
    return engineCallOverdue(method, elapsed, kShortCeiling) ||
           engineCallOverdueLong(method, elapsed, kLongCeiling);
}

}  // namespace

TEST_CASE("modeling calls get only the long ceiling") {
    // Rebuild cost scales with input size, including clone preflight and cold
    // activation. Surviving the short deadline must not make these unbounded.
    for (const char* method : {"doc_apply_ops", "doc_rebuild", "doc_undo", "doc_redo",
                               "doc_activate", "doc_regen", "run_fea_static",
                               "run_cfd_flow", "cam_generate", "doc_save", "doc_open",
                               "export_part", "import_part"}) {
        CAPTURE(method);
        CHECK_FALSE(overdue(method, kShortCeiling + 1));
        CHECK_FALSE(overdue(method, 600000));  // ten-minute valid long operation
        CHECK_FALSE(overdue(method, kLongCeiling));
        CHECK(overdue(method, kLongCeiling + 1));
        CHECK_FALSE(engineCallOverdue(method, kLongCeiling + 1, kShortCeiling));
        CHECK_FALSE(engineCallOverdueLong(method, kLongCeiling + 1, 0));
        CHECK_FALSE(engineCallOverdueLong(method, kLongCeiling + 1, -1));
    }
}

TEST_CASE("agent and chudcode turns have no wall-clock cap") {
    // A turn streams progress for hours; its worker transport, not this watchdog,
    // owns silence/absolute limits.
    for (const char* method : {"agent_run", "agent_admin_turn", "agent_step",
                               "chudcode_export_session"}) {
        CAPTURE(method);
        CHECK_FALSE(overdue(method, kLongCeiling + 1));
        CHECK_FALSE(overdue(method, 4 * kLongCeiling));
        CHECK_FALSE(overdue(method, 48 * kLongCeiling));
    }
}

TEST_CASE("bounded calls keep the short ceiling") {
    for (const char* method : {"ping", "__hostd_test_stall"}) {
        CAPTURE(method);
        CHECK_FALSE(overdue(method, 5000));
        CHECK_FALSE(overdue(method, kShortCeiling));
        CHECK(overdue(method, kShortCeiling + 1));
        CHECK_FALSE(engineCallOverdueLong(method, kLongCeiling + 1, kLongCeiling));
        CHECK_FALSE(engineCallOverdue(method, kLongCeiling + 1, 0));
        CHECK_FALSE(engineCallOverdue(method, kLongCeiling + 1, -1));
    }
}
