// doctest unit tests for aicad::util::evaluate — the dimension-expression
// arithmetic evaluator (C++ port of engine/doc/equations.py `evaluate`).
//
// Expected values below were captured from the Python oracle (equations.evaluate)
// so this test doubles as a parity guard. Coverage:
//   - binary + - * / and ** / ^ with Python precedence (^ == ** precedence)
//   - unary minus interacting with ** (-2**2 == -4, 2**-1 == 0.5)
//   - right-associative ** (2**3**2 == 512, 2^3^2 == 512)
//   - parentheses, names from env, the `pi` constant
//   - whitelisted functions: sqrt, abs, min/max (varargs), floor, ceil, and
//     degrees-in/out trig (sin/cos/tan/asin/acos/atan)
//   - error cases: unknown name, unknown function, div-by-zero, empty, bad syntax
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "util/expr.h"

#include <map>
#include <string>

using aicad::util::evaluate;
using aicad::util::ExprError;

static const std::map<std::string, double> kEnv = {
    {"A", 3.0}, {"B", 4.0}, {"H", 10.0}, {"W", 80.0}, {"R", 5.0}};

TEST_CASE("basic arithmetic and precedence") {
    CHECK(evaluate("A*B", kEnv) == doctest::Approx(12.0));
    CHECK(evaluate("W/2", kEnv) == doctest::Approx(40.0));
    CHECK(evaluate("H/2", kEnv) == doctest::Approx(5.0));
    CHECK(evaluate("A + B*2", kEnv) == doctest::Approx(11.0));     // * before +
    CHECK(evaluate("(A+B)*2", kEnv) == doctest::Approx(14.0));     // parens
    CHECK(evaluate("3 + 4 * 2 - 1", {}) == doctest::Approx(10.0));
    CHECK(evaluate("2*pi*R", kEnv) == doctest::Approx(31.41592653589793));
    CHECK(evaluate("pi", {}) == doctest::Approx(3.141592653589793));
}

TEST_CASE("power: right-assoc, caret-maps-to-power, unary-minus interaction") {
    CHECK(evaluate("-A**2", kEnv) == doctest::Approx(-9.0));   // -(A**2)
    CHECK(evaluate("2**-1", {}) == doctest::Approx(0.5));      // unary - in exponent
    CHECK(evaluate("-2**2", {}) == doctest::Approx(-4.0));     // -(2**2)
    CHECK(evaluate("2 ** 3 ** 2", {}) == doctest::Approx(512.0));  // right-assoc
    CHECK(evaluate("2^3^2", {}) == doctest::Approx(512.0));        // ^ == ** prec
    CHECK(evaluate("2^10", {}) == doctest::Approx(1024.0));
}

TEST_CASE("whitelisted functions") {
    CHECK(evaluate("sqrt(A*A+B*B)", kEnv) == doctest::Approx(5.0));
    CHECK(evaluate("abs(-5)", {}) == doctest::Approx(5.0));
    CHECK(evaluate("min(A,B,1)", kEnv) == doctest::Approx(1.0));
    CHECK(evaluate("max(A,B)", kEnv) == doctest::Approx(4.0));
    CHECK(evaluate("floor(3.7)", {}) == doctest::Approx(3.0));
    CHECK(evaluate("ceil(3.2)", {}) == doctest::Approx(4.0));
}

TEST_CASE("trig is in degrees, matching Python within 1e-9") {
    // Python: sin(30)=0.49999999999999994, asin(0.5)=30.000000000000004
    CHECK(evaluate("sin(30)", {}) == doctest::Approx(0.5).epsilon(1e-9));
    CHECK(evaluate("cos(0)", {}) == doctest::Approx(1.0));
    CHECK(evaluate("tan(45)", {}) == doctest::Approx(1.0).epsilon(1e-9));
    CHECK(evaluate("asin(0.5)", {}) == doctest::Approx(30.0).epsilon(1e-9));
    CHECK(evaluate("acos(1)", {}) == doctest::Approx(0.0).epsilon(1e-9));
    CHECK(evaluate("atan(1)", {}) == doctest::Approx(45.0).epsilon(1e-9));
}

TEST_CASE("scientific notation literals") {
    CHECK(evaluate("1.5e2", {}) == doctest::Approx(150.0));
    CHECK(evaluate("2E-1", {}) == doctest::Approx(0.2));
    CHECK(evaluate("1e3+2", {}) == doctest::Approx(1002.0));
}

TEST_CASE("error cases throw ExprError") {
    CHECK_THROWS_AS(evaluate("NOPE", {}), ExprError);        // unknown name
    CHECK_THROWS_AS(evaluate("bogus(1)", {}), ExprError);    // unknown function
    CHECK_THROWS_AS(evaluate("1/0", {}), ExprError);         // div by zero
    CHECK_THROWS_AS(evaluate("", {}), ExprError);            // empty
    CHECK_THROWS_AS(evaluate("   ", {}), ExprError);         // blank
    CHECK_THROWS_AS(evaluate("1 + ", {}), ExprError);        // trailing op
    CHECK_THROWS_AS(evaluate("(1 + 2", {}), ExprError);      // unbalanced paren
    CHECK_THROWS_AS(evaluate("sqrt(-1)", {}), ExprError);    // domain error
}
