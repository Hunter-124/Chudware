// Unit tests for agent/orchestrator/machine.cpp (err_sig identical-failure detector).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/machine.h"

using aicad::agent::err_sig;

TEST_CASE("err_sig picks the last exception line") {
    std::string err = "line one\nValueError: bad dim\nline three";
    CHECK(err_sig(err) == "ValueError: bad dim");
}

TEST_CASE("err_sig falls back to last nonempty line") {
    std::string err = "first\nsecond\n";
    CHECK(err_sig(err) == "second");
}

TEST_CASE("err_sig empty input") {
    CHECK(err_sig("") == "");
}

TEST_CASE("err_sig matches dotted exception names") {
    std::string err = "pkg.mod.CustomError: detail";
    CHECK(err_sig(err) == "pkg.mod.CustomError: detail");
}
